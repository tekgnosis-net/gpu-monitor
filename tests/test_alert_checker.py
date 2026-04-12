"""
Tests for reporting.alert_checker — server-side GPU alert state machine.

Tests the threshold evaluation, cooldown logic, and dispatch integration
using a fixture SQLite database with controlled metric values. No real
HTTP or SMTP calls — the notifiers.dispatch_alert function is mocked.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from reporting import alert_checker


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _seed_db(db_path: Path, rows: list[dict]) -> None:
    """Create a gpu_metrics table and insert test rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpu_metrics (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            timestamp_epoch INTEGER NOT NULL,
            temperature     REAL,
            utilization     REAL,
            memory          REAL,
            power           REAL,
            gpu_index       INTEGER NOT NULL DEFAULT 0,
            gpu_uuid        TEXT,
            interval_s      INTEGER NOT NULL DEFAULT 4
        )
    """)
    for row in rows:
        conn.execute("""
            INSERT INTO gpu_metrics
            (timestamp, timestamp_epoch, temperature, utilization, memory, power, gpu_index)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            row.get("timestamp", "2026-04-12 10:00:00"),
            row.get("timestamp_epoch", 1712920800),
            row.get("temperature", 50),
            row.get("utilization", 10),
            row.get("memory", 5000),
            row.get("power", 100),
            row.get("gpu_index", 0),
        ))
    conn.commit()
    conn.close()


def _make_settings(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a settings.json with alert channels configured."""
    settings = {
        "alerts": {
            "temperature_c": 80,
            "utilization_pct": 90,
            "power_w": 300,
            "cooldown_seconds": 60,
            "poll_interval_seconds": 30,
            "channels": {
                "ntfy": {"enabled": True, "topic_url": "https://ntfy.sh/test"},
                "pushover": {"enabled": False},
                "webhook": {"enabled": False},
                "email": {"enabled": False},
            },
        },
        "smtp": {"host": "", "port": 587, "user": "", "password_enc": "", "from": "", "tls": "starttls"},
    }
    if overrides:
        for key, val in overrides.items():
            if isinstance(val, dict) and key in settings:
                settings[key].update(val)
            else:
                settings[key] = val
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    return path


def _make_inventory(tmp_path: Path, gpus: list[dict] | None = None) -> Path:
    """Write a gpu_inventory.json."""
    if gpus is None:
        gpus = [{"index": 0, "uuid": "GPU-abc", "name": "RTX 3090"}]
    path = tmp_path / "gpu_inventory.json"
    path.write_text(json.dumps({"gpus": gpus}))
    return path


def _make_state(tmp_path: Path, settings_overrides: dict | None = None,
                gpu_rows: list[dict] | None = None,
                gpus: list[dict] | None = None) -> alert_checker._AlertCheckerState:
    """Build a complete test state with DB, settings, inventory."""
    state = alert_checker._AlertCheckerState(base_dir=tmp_path)
    state.settings_file = _make_settings(tmp_path, settings_overrides)
    state.db_file = tmp_path / "gpu_metrics.db"
    _seed_db(state.db_file, gpu_rows or [{"gpu_index": 0, "temperature": 50, "power": 100}])
    state.inventory_file = _make_inventory(tmp_path, gpus)
    state.secret_key_file = tmp_path / ".secret"
    return state


# ─── Threshold tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_below_threshold_no_alert(tmp_path):
    """When all metrics are below thresholds, no alerts fire."""
    state = _make_state(tmp_path, gpu_rows=[
        {"gpu_index": 0, "temperature": 70, "utilization": 50, "power": 200,
         "timestamp_epoch": 1712920800},
    ])
    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]) as mock_dispatch:
        fired = await alert_checker.run_once(state)

    assert fired == 0
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_above_threshold_fires_alert(tmp_path):
    """When temperature exceeds threshold, an alert fires."""
    state = _make_state(tmp_path, gpu_rows=[
        {"gpu_index": 0, "temperature": 85, "utilization": 50, "power": 200,
         "timestamp_epoch": 1712920800},
    ])
    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]) as mock_dispatch, \
         patch("reporting.alert_checker.crypto") as mock_crypto:
        mock_crypto.load_or_create_key.return_value = b"fake_key"
        fired = await alert_checker.run_once(state)

    assert fired == 1
    mock_dispatch.assert_called_once()
    call_kwargs = mock_dispatch.call_args[1]
    assert call_kwargs["alert_data"]["metric"] == "Temperature"
    assert call_kwargs["alert_data"]["value"] == 85.0


@pytest.mark.asyncio
async def test_multiple_metrics_over_threshold(tmp_path):
    """Multiple metrics exceeding threshold fire separate alerts."""
    state = _make_state(tmp_path, gpu_rows=[
        {"gpu_index": 0, "temperature": 85, "utilization": 95, "power": 350,
         "timestamp_epoch": 1712920800},
    ])
    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]) as mock_dispatch, \
         patch("reporting.alert_checker.crypto") as mock_crypto:
        mock_crypto.load_or_create_key.return_value = b"fake_key"
        fired = await alert_checker.run_once(state)

    assert fired == 3  # temperature + utilization + power
    assert mock_dispatch.call_count == 3


# ─── Cooldown tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_suppresses_repeat_fire(tmp_path):
    """A second tick within the cooldown window should NOT fire again."""
    state = _make_state(tmp_path, gpu_rows=[
        {"gpu_index": 0, "temperature": 85, "timestamp_epoch": 1712920800},
    ])

    now = 1712920800.0

    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]) as mock_dispatch, \
         patch("reporting.alert_checker.crypto") as mock_crypto:
        mock_crypto.load_or_create_key.return_value = b"fake_key"

        # First tick: should fire
        fired1 = await alert_checker.run_once(state, now_epoch=now)
        assert fired1 == 1

        # Second tick 10s later (cooldown is 60s): should suppress
        fired2 = await alert_checker.run_once(state, now_epoch=now + 10)
        assert fired2 == 0

    # dispatch was called only once (the first fire)
    assert mock_dispatch.call_count == 1


@pytest.mark.asyncio
async def test_cooldown_expires_allows_refire(tmp_path):
    """After cooldown expires and metric is still over threshold, re-fire."""
    state = _make_state(tmp_path, gpu_rows=[
        {"gpu_index": 0, "temperature": 85, "timestamp_epoch": 1712920800},
    ])

    now = 1712920800.0

    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]) as mock_dispatch, \
         patch("reporting.alert_checker.crypto") as mock_crypto:
        mock_crypto.load_or_create_key.return_value = b"fake_key"

        # First tick: fire
        await alert_checker.run_once(state, now_epoch=now)
        # After cooldown (60s): should re-fire
        fired = await alert_checker.run_once(state, now_epoch=now + 61)

    assert fired == 1
    assert mock_dispatch.call_count == 2


@pytest.mark.asyncio
async def test_below_threshold_clears_cooldown(tmp_path):
    """When metric drops below threshold, cooldown is cleared so the
    next breach fires immediately without waiting for the window."""
    state = _make_state(tmp_path)

    # Seed cooldown as if we fired 5 seconds ago
    state.last_fire[(0, "temperature")] = 1712920800.0

    # Now the DB has temperature=50 (below 80 threshold)
    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]), \
         patch("reporting.alert_checker.crypto") as mock_crypto:
        mock_crypto.load_or_create_key.return_value = b"fake_key"
        await alert_checker.run_once(state, now_epoch=1712920805.0)

    # Cooldown should be cleared
    assert (0, "temperature") not in state.last_fire


# ─── Multi-GPU tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_gpu_independent_cooldown(tmp_path):
    """GPU 0 fires, GPU 1 fires independently — cooldowns don't interfere."""
    state = _make_state(
        tmp_path,
        gpu_rows=[
            {"gpu_index": 0, "temperature": 85, "timestamp_epoch": 1712920800},
            {"gpu_index": 1, "temperature": 85, "timestamp_epoch": 1712920800},
        ],
        gpus=[
            {"index": 0, "uuid": "GPU-0", "name": "RTX 3090 #0"},
            {"index": 1, "uuid": "GPU-1", "name": "RTX 3090 #1"},
        ],
    )

    # Pre-set cooldown for GPU 0 only
    state.last_fire[(0, "temperature")] = 1712920795.0  # 5s ago

    with patch("reporting.notifiers.dispatch_alert", new_callable=AsyncMock, return_value=["ntfy"]) as mock_dispatch, \
         patch("reporting.alert_checker.crypto") as mock_crypto:
        mock_crypto.load_or_create_key.return_value = b"fake_key"

        fired = await alert_checker.run_once(state, now_epoch=1712920800.0)

    # GPU 0 in cooldown → suppressed. GPU 1 → fires.
    assert fired == 1
    call_kwargs = mock_dispatch.call_args[1]
    assert call_kwargs["alert_data"]["gpu_index"] == 1


# ─── No channels enabled ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_channels_enabled_skips_everything(tmp_path):
    """When no channels are enabled, run_once short-circuits before DB query."""
    state = _make_state(tmp_path, settings_overrides={
        "alerts": {
            "temperature_c": 80, "utilization_pct": 90, "power_w": 300,
            "cooldown_seconds": 60, "poll_interval_seconds": 30,
            "channels": {
                "ntfy": {"enabled": False},
                "pushover": {"enabled": False},
                "webhook": {"enabled": False},
                "email": {"enabled": False},
            },
        },
    })

    with patch("reporting.alert_checker._get_latest_metrics") as mock_query:
        fired = await alert_checker.run_once(state)

    assert fired == 0
    mock_query.assert_not_called()  # early exit before DB access
