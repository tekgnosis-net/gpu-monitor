"""Tests for `gpu_monitor.housekeeping`.

Two functions under test (`rotate_logs`, `clean_old_data`) plus the
async tick loop (`run`). The functions are deterministic given a
filesystem state + settings file; the loop is exercised with a
patched `asyncio.sleep` to drive simulated time forward fast.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from gpu_monitor import db, housekeeping


# ─── rotate_logs ───────────────────────────────────────────────────────────


def test_rotate_logs_size_threshold(tmp_path):
    """A `.log` file larger than max_size gets renamed with a
    timestamp suffix; a fresh empty file replaces it."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"logging": {"max_size_mb": 1, "max_age_hours": 25}}))

    big_log = log_dir / "app.log"
    big_log.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MiB > 1 MiB threshold

    housekeeping.rotate_logs(log_dir=log_dir, settings_path=settings)

    # Original file is fresh / smaller now (truncated by touch)
    assert big_log.exists()
    assert big_log.stat().st_size == 0
    # Exactly one rotated file with the expected naming convention
    rotated = list(log_dir.glob("app.log.*"))
    assert len(rotated) == 1
    # Format: app.log.YYYYMMDD-HHMMSS
    parts = rotated[0].name.split(".")
    assert parts[0] == "app"
    assert parts[1] == "log"
    assert len(parts[2]) == 15  # YYYYMMDD-HHMMSS


def test_rotate_logs_skips_small_files(tmp_path):
    """A file under the size threshold is left untouched."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")

    small_log = log_dir / "app.log"
    small_log.write_bytes(b"x" * 1024)  # 1 KiB, well under default 5 MiB

    housekeeping.rotate_logs(log_dir=log_dir, settings_path=settings)

    assert small_log.stat().st_size == 1024
    assert not list(log_dir.glob("app.log.*"))


def test_rotate_logs_age_based_cleanup(tmp_path):
    """Rotated files older than max_age_hours are deleted."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"logging": {"max_size_mb": 5, "max_age_hours": 1}}))

    old_rotated = log_dir / "app.log.20260101-000000"
    old_rotated.write_text("ancient")
    # Backdate the file 2 hours
    old_mtime = time.time() - 2 * 3600
    os.utime(old_rotated, (old_mtime, old_mtime))

    fresh_rotated = log_dir / "app.log.20260430-130000"
    fresh_rotated.write_text("recent")

    housekeeping.rotate_logs(log_dir=log_dir, settings_path=settings)

    assert not old_rotated.exists()       # > 1h old, deleted
    assert fresh_rotated.exists()          # fresh, kept


def test_rotate_logs_no_log_dir(tmp_path):
    """Missing log_dir → no-op, no exception."""
    housekeeping.rotate_logs(
        log_dir=tmp_path / "nonexistent",
        settings_path=tmp_path / "settings.json",
    )


def test_rotate_logs_clamps_invalid_settings(tmp_path):
    """Out-of-range max_size_mb falls back to default 5 MiB. We
    verify by setting a 1KB file and confirming it's NOT rotated
    (default 5 MiB threshold), proving the invalid 0 was rejected."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"logging": {"max_size_mb": 0}}))  # invalid

    f = log_dir / "app.log"
    f.write_bytes(b"x" * 1024)

    housekeeping.rotate_logs(log_dir=log_dir, settings_path=settings)
    # Invalid 0 → default 5 MiB → 1KB doesn't trigger rotation
    assert not list(log_dir.glob("app.log.*"))


# ─── clean_old_data ─────────────────────────────────────────────────────────


def test_clean_old_data_deletes_old_rows(tmp_path):
    """Rows older than retention_days + 10min slack are deleted;
    fresh rows survive."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"housekeeping": {"retention_days": 3}}))

    now = int(time.time())
    # 5 days old (well past retention)
    old_ts = now - (5 * 86400)
    # 1 day old (well within retention)
    fresh_ts = now - 86400

    conn = sqlite3.connect(str(db_path))
    try:
        for ts, label in [(old_ts, "old"), (fresh_ts, "fresh")]:
            conn.execute("""
                INSERT INTO gpu_metrics
                    (timestamp, timestamp_epoch, temperature, utilization,
                     memory, power, gpu_index, gpu_uuid, interval_s)
                VALUES (?, ?, 60, 50, 8000, 200, 0, ?, 4)
            """, (label, ts, label))
        conn.commit()
    finally:
        conn.close()

    housekeeping.clean_old_data(db_path=db_path, settings_path=settings)

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute("SELECT gpu_uuid FROM gpu_metrics"))
        assert rows == [("fresh",)]
    finally:
        conn.close()


def test_clean_old_data_default_retention(tmp_path):
    """Missing settings.json → default retention_days=3 applied."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)
    # No settings file written

    now = int(time.time())
    conn = sqlite3.connect(str(db_path))
    try:
        # Row 4 days old — past 3-day default
        old_ts = now - (4 * 86400)
        conn.execute("""
            INSERT INTO gpu_metrics
                (timestamp, timestamp_epoch, temperature, utilization,
                 memory, power, gpu_index, gpu_uuid, interval_s)
            VALUES ('old', ?, 60, 50, 8000, 200, 0, 'old', 4)
        """, (old_ts,))
        conn.commit()
    finally:
        conn.close()

    housekeeping.clean_old_data(
        db_path=db_path,
        settings_path=tmp_path / "missing.json",
    )

    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM gpu_metrics").fetchone()[0]
        assert count == 0
    finally:
        conn.close()


# ─── async run loop ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_fires_rotation_on_first_tick(tmp_path, monkeypatch):
    """The first tick always rotates (last_rotation_hour starts at
    None), regardless of the current hour."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"logging": {"max_size_mb": 1}}))
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)

    big = log_dir / "app.log"
    big.write_bytes(b"x" * (2 * 1024 * 1024))

    sleeps = []

    async def fast_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(housekeeping.asyncio, "sleep", fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await housekeeping.run(
            log_dir=log_dir, db_path=db_path,
            settings_path=settings, tick_seconds=0.01,
        )

    # Rotation fired on tick 1
    assert big.stat().st_size == 0
    assert len(list(log_dir.glob("app.log.*"))) == 1


@pytest.mark.asyncio
async def test_run_skips_purge_outside_hour_zero(tmp_path, monkeypatch):
    """Purge is gated on hour=0; outside that, only rotation fires."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)

    # Insert ancient row to confirm whether purge ran
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            INSERT INTO gpu_metrics
                (timestamp, timestamp_epoch, temperature, utilization,
                 memory, power, gpu_index, gpu_uuid, interval_s)
            VALUES ('ancient', 0, 60, 50, 8000, 200, 0, 'a', 4)
        """)
        conn.commit()
    finally:
        conn.close()

    # Patch datetime.now to return an hour != 0
    fixed = datetime(2026, 4, 30, 14, 30, 0)

    class FixedDateTime:
        @classmethod
        def now(cls): return fixed

    monkeypatch.setattr(housekeeping, "datetime", FixedDateTime)

    sleeps = []

    async def fast_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(housekeeping.asyncio, "sleep", fast_sleep)

    with pytest.raises(asyncio.CancelledError):
        await housekeeping.run(
            log_dir=log_dir, db_path=db_path,
            settings_path=settings, tick_seconds=0.01,
        )

    # Ancient row still present — purge did NOT run
    conn = sqlite3.connect(str(db_path))
    try:
        count = conn.execute("SELECT COUNT(*) FROM gpu_metrics").fetchone()[0]
        assert count == 1
    finally:
        conn.close()
