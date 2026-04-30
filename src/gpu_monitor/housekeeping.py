"""Periodic maintenance tasks: log rotation + DB purge.

Replaces the bash `rotate_logs()` (hourly, size+age based) and
`clean_old_data()` (daily at 00:00, retention_days from settings).

Schedule logic
--------------

The loop ticks every 60s and checks two boundaries:

  * **Hour boundary** — if the current hour differs from the last
    rotation hour, run rotate_logs.
  * **Day boundary at 00:00** — if the current date differs from
    the last purge date AND we're in hour 0, run clean_old_data.
    The hour-0 guard prevents the purge from firing on the very
    first tick of a fresh container start (which could be at any
    hour) and keeps it aligned with the legacy 00:00 schedule.

A single 60s loop is used (rather than two `asyncio.sleep` until-
boundary tasks) because the work is light and the shutdown story is
simpler — a single `asyncio.CancelledError` propagation point.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path

log = logging.getLogger("gpu-monitor.housekeeping")


# Validation bounds matching legacy bash defaults.
DEFAULT_MAX_SIZE_MB = 5
DEFAULT_MAX_AGE_HOURS = 25
DEFAULT_RETENTION_DAYS = 3
RETENTION_SLACK_S = 600  # 10 min slack to match legacy RETENTION_SECONDS

# Suffix pattern for rotated files: gpu_stats.log.20260430-143012
_ROTATED_SUFFIX_LEN = len(".YYYYMMDD-HHMMSS")  # 16 chars


# ─── Settings reader ───────────────────────────────────────────────────────


def _load_settings(settings_path: str | Path) -> dict:
    """Read settings.json, returning {} on any failure. The bash
    version's defaults are encoded by the caller, not here."""
    try:
        with Path(settings_path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _bounded_int(value, default: int, lo: int, hi: int) -> int:
    """Clamp an integer value to [lo, hi], falling back to default
    on any non-int / out-of-range input. Matches bash regex+range
    validation in load_settings()/rotate_logs()/clean_old_data()."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo or n > hi:
        return default
    return n


# ─── Log rotation ──────────────────────────────────────────────────────────


def rotate_logs(*, log_dir: str | Path, settings_path: str | Path) -> None:
    """Size + age based rotation of every `*.log` file in `log_dir`.

    Settings:
      logging.max_size_mb  — int [1, 100], default 5
      logging.max_age_hours — int [1, 720], default 25

    Already-rotated files (`*.log.YYYYMMDD-HHMMSS`) are not re-rotated;
    their age is checked against max_age and they're deleted if too
    old. This matches the legacy bash logic exactly.
    """
    settings = _load_settings(settings_path)
    logging_cfg = settings.get("logging", {}) or {}
    max_size_mb = _bounded_int(
        logging_cfg.get("max_size_mb"), DEFAULT_MAX_SIZE_MB, 1, 100,
    )
    max_age_hours = _bounded_int(
        logging_cfg.get("max_age_hours"), DEFAULT_MAX_AGE_HOURS, 1, 720,
    )

    max_size_bytes = max_size_mb * 1024 * 1024
    max_age_seconds = max_age_hours * 3600
    now_epoch = int(time.time())

    log_dir_path = Path(log_dir)
    if not log_dir_path.is_dir():
        return

    # Active logs (size-based rotation candidates) and rotated files
    # (age-based deletion candidates) are scanned separately so a
    # newly-rotated file isn't immediately considered for deletion.
    for log_file in sorted(log_dir_path.glob("*.log")):
        try:
            size = log_file.stat().st_size
        except OSError:
            continue
        if size > max_size_bytes:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = log_file.with_name(f"{log_file.name}.{timestamp}")
            try:
                log_file.rename(target)
                log_file.touch()
                log.debug("rotated %s due to size (>%d bytes)", log_file, max_size_bytes)
            except OSError as exc:
                log.warning("rotate_logs: failed to rotate %s (%s)", log_file, exc)

    # Age-based deletion of already-rotated files. Match any file whose
    # name has a base `*.log` followed by a `.timestamp` suffix.
    for rotated in log_dir_path.iterdir():
        if not rotated.is_file():
            continue
        # Filter: must look like "<base>.log.<timestamp>"
        parts = rotated.name.rsplit(".", 1)
        if len(parts) != 2 or not parts[0].endswith(".log"):
            continue
        try:
            mtime = rotated.stat().st_mtime
        except OSError:
            continue
        age = now_epoch - int(mtime)
        if age > max_age_seconds:
            try:
                rotated.unlink()
                log.debug("removed old log: %s (age %ds > %ds)",
                          rotated, age, max_age_seconds)
            except OSError as exc:
                log.warning("rotate_logs: failed to delete %s (%s)", rotated, exc)


# ─── DB purge ──────────────────────────────────────────────────────────────


def clean_old_data(*, db_path: str | Path, settings_path: str | Path) -> None:
    """Delete gpu_metrics rows older than retention_days + 10min slack.

    Settings:
      housekeeping.retention_days — int [1, 365], default 3

    The 10-minute slack preserves legacy behavior exactly: a row
    sampled at "now - retention_days - 5 minutes" stays visible in
    the last-24h chart until the next daily sweep, rather than
    being shaved off by a strict boundary. Imperceptible in practice
    but explicitly part of the v1.x contract.
    """
    settings = _load_settings(settings_path)
    housekeeping_cfg = settings.get("housekeeping", {}) or {}
    retention_days = _bounded_int(
        housekeeping_cfg.get("retention_days"), DEFAULT_RETENTION_DAYS, 1, 365,
    )

    retention_seconds = retention_days * 86400 + RETENTION_SLACK_S
    cutoff = int(time.time()) - retention_seconds

    log.info(
        "purge: deleting gpu_metrics rows older than %d days (cutoff epoch %d)",
        retention_days, cutoff,
    )

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "DELETE FROM gpu_metrics WHERE timestamp_epoch < ?", (cutoff,)
        )
        deleted = cur.rowcount
        conn.commit()
        # VACUUM cannot run in a transaction
        conn.isolation_level = None
        conn.execute("VACUUM")
        log.info("purge: deleted %d row(s) and VACUUMed", deleted)
    except sqlite3.Error as exc:
        log.error("purge: failed (%s)", exc)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
    finally:
        conn.close()


# ─── Async run loop ────────────────────────────────────────────────────────


async def run(
    *,
    log_dir: str | Path,
    db_path: str | Path,
    settings_path: str | Path,
    tick_seconds: float = 60.0,
) -> None:
    """Async run loop: tick every `tick_seconds`, fire rotation
    on hour boundaries and purge on day boundaries at 00:00.

    `tick_seconds` is configurable for tests (which can pass e.g.
    0.01 to drive many simulated boundaries quickly).
    """
    last_rotation_hour: int | None = None
    last_purge_date: date | None = None

    log.info("housekeeping: started (tick=%ss)", tick_seconds)
    try:
        while True:
            now = datetime.now()

            if now.hour != last_rotation_hour:
                try:
                    rotate_logs(log_dir=log_dir, settings_path=settings_path)
                except Exception:
                    log.exception("housekeeping: rotate_logs failed")
                last_rotation_hour = now.hour

            if now.hour == 0 and now.date() != last_purge_date:
                try:
                    clean_old_data(db_path=db_path, settings_path=settings_path)
                except Exception:
                    log.exception("housekeeping: clean_old_data failed")
                last_purge_date = now.date()

            await asyncio.sleep(tick_seconds)
    except asyncio.CancelledError:
        log.info("housekeeping: stopped")
        raise
