"""
Tests for reporting.scheduler.

Scope:
  * _is_due correctly identifies past-due schedules
  * _is_due doesn't fire for the same slot twice
  * _is_due doesn't fire multiple times on backlog recovery
  * run_once skips schedules with empty SMTP host
  * run_once skips schedules with no recipients
  * run_once skips disabled schedules
  * run_once fires enabled/due schedule and updates last_run_epoch
  * invalid cron expression is rejected without crashing the loop

The tests DO exercise the full render → mailer → send path using
the same inline asyncio SMTP listener from test_mailer.py, and a
minimal seeded DB. This ensures the three modules actually
interoperate — the plan warned that "moving parts" coupling is
the biggest risk in Phase 6, so the integration test is worth
the complexity.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from reporting import scheduler  # noqa: E402


# Reuse the inline SMTP listener from test_mailer.py
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_mailer import _handle_smtp_client, SmtpCatcher  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def smtp_server():
    """Copy of test_mailer's fixture — imported here so the scheduler
    test doesn't depend on test_mailer running first."""
    catcher = SmtpCatcher()
    server = await asyncio.start_server(
        lambda r, w: _handle_smtp_client(r, w, catcher),
        host="127.0.0.1",
        port=0,
    )
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]

    async def _serve():
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_serve())
    try:
        yield (host, port, catcher)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


@pytest.fixture
def scheduler_env(tmp_path, smtp_server):
    """Build a stand-in /app with settings.json + seeded DB +
    inventory + version, plus an _SchedulerState pointing at it.

    Returns a dict: {state, tmp_base, smtp_host, smtp_port, catcher}.
    """
    base = tmp_path / "app"
    base.mkdir()
    history = base / "history"
    history.mkdir()

    (base / "VERSION").write_text("1.0.0-test\n")

    # Seeded DB with 50 rows per GPU
    db_path = history / "gpu_metrics.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE gpu_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            timestamp_epoch INTEGER NOT NULL,
            temperature REAL NOT NULL,
            utilization REAL NOT NULL,
            memory REAL NOT NULL,
            power REAL NOT NULL,
            gpu_index INTEGER NOT NULL DEFAULT 0,
            gpu_uuid TEXT,
            interval_s INTEGER NOT NULL DEFAULT 4
        );
    """)
    conn.execute("CREATE INDEX idx_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);")
    now = int(datetime.now(timezone.utc).timestamp())
    for i in range(50):
        ts_epoch = now - (49 - i) * 60
        ts_str = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO gpu_metrics
               (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                gpu_index, gpu_uuid, interval_s)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, 4)""",
            (ts_str, ts_epoch, 60.0 + i * 0.1, 50.0, 8192.0, 200.0 + i * 0.5, "GPU-0"),
        )
    conn.commit()
    conn.close()

    inv_path = base / "gpu_inventory.json"
    inv_path.write_text(json.dumps({
        "gpus": [{"index": 0, "name": "Test Card", "uuid": "GPU-0",
                  "memory_total_mib": 24576, "power_limit_w": 450}],
    }))

    # settings.json with SMTP pointed at the test listener and
    # one disabled + one enabled daily schedule
    host, port, _catcher = smtp_server
    settings_path = base / "settings.json"
    settings_data = {
        "collection": {"interval_seconds": 4, "flush_interval_seconds": 60},
        "housekeeping": {"retention_days": 3},
        "logging": {"max_size_mb": 5, "max_age_hours": 25},
        "alerts": {
            "temperature_c": 80, "utilization_pct": 100, "power_w": 300,
            "cooldown_seconds": 10, "sound_enabled": True,
            "notifications_enabled": False,
        },
        "power": {"rate_per_kwh": 0.15, "currency": "$"},
        "smtp": {
            "host": host, "port": port,
            "user": "", "password_enc": "",
            "from": "scheduler@test.local",
            "tls": "none",
        },
        "schedules": [],
        "theme": {"default_mode": "auto"},
    }
    settings_path.write_text(json.dumps(settings_data, indent=2))

    state = scheduler._SchedulerState(base_dir=base)
    state.tz = ZoneInfo("UTC")  # deterministic across container TZ

    return {
        "state": state,
        "tmp_base": base,
        "smtp_host": host,
        "smtp_port": port,
        "catcher": smtp_server[2],
        "settings_path": settings_path,
    }


def _add_schedule(settings_path: Path, **overrides) -> None:
    """Append / replace the schedule list in settings.json with a
    single entry built from overrides + sensible defaults."""
    data = json.loads(settings_path.read_text())
    entry = {
        "id": "daily-0800",
        "template": "daily",
        "cron": "0 8 * * *",
        "recipients": ["ops@test.local"],
        "enabled": True,
        "last_run_epoch": None,
    }
    entry.update(overrides)
    data["schedules"] = [entry]
    settings_path.write_text(json.dumps(data, indent=2))


# ─── _is_due unit tests ───────────────────────────────────────────────────


def test_is_due_never_run_fires_on_first_tick():
    """A schedule that's never run (last_run_epoch=None) with a cron
    that has a past slot fires on first tick."""
    tz = ZoneInfo("UTC")
    # now = 2026-04-12 09:00 UTC; cron "0 8 * * *" last fired at 08:00
    now = int(datetime(2026, 4, 12, 9, 0, tzinfo=tz).timestamp())
    assert scheduler._is_due("0 8 * * *", None, now, tz) is True


def test_is_due_fires_after_last_run_but_not_before():
    """last_run_epoch at yesterday 08:00, now at today 09:00 → due."""
    tz = ZoneInfo("UTC")
    yesterday_0800 = int(datetime(2026, 4, 11, 8, 0, tzinfo=tz).timestamp())
    today_0900 = int(datetime(2026, 4, 12, 9, 0, tzinfo=tz).timestamp())
    assert scheduler._is_due("0 8 * * *", yesterday_0800, today_0900, tz) is True


def test_is_due_not_fired_twice_for_same_slot():
    """last_run_epoch at today 08:00, now at today 09:00 → NOT due
    (already ran at the most recent scheduled time)."""
    tz = ZoneInfo("UTC")
    today_0800 = int(datetime(2026, 4, 12, 8, 0, tzinfo=tz).timestamp())
    today_0900 = int(datetime(2026, 4, 12, 9, 0, tzinfo=tz).timestamp())
    assert scheduler._is_due("0 8 * * *", today_0800, today_0900, tz) is False


def test_is_due_backlog_fires_only_once_not_n_times():
    """A container offline for 3 days comes up, sees yesterday 08:00
    as the most-recent scheduled slot (not 3 days ago's 08:00),
    fires once. Asserts that catch-up is one-shot, not N-shot."""
    tz = ZoneInfo("UTC")
    four_days_ago = int(datetime(2026, 4, 8, 7, 0, tzinfo=tz).timestamp())
    now = int(datetime(2026, 4, 12, 9, 0, tzinfo=tz).timestamp())
    # get_prev returns 2026-04-12 08:00 (today), NOT 2026-04-09 08:00
    # → last_run_epoch=2026-04-08 is strictly less → due (fires once)
    assert scheduler._is_due("0 8 * * *", four_days_ago, now, tz) is True

    # After the fire, last_run_epoch = now (2026-04-12 09:00).
    # Next tick at 2026-04-12 09:01 should NOT fire.
    slightly_later = now + 60
    assert scheduler._is_due("0 8 * * *", now, slightly_later, tz) is False


def test_is_due_invalid_cron_returns_false():
    """A garbage cron string logs a warning and returns False — it
    must not crash the scheduler loop."""
    tz = ZoneInfo("UTC")
    assert scheduler._is_due("not a real cron", None, 1000, tz) is False


# ─── run_once integration tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_once_empty_schedules_is_noop(scheduler_env):
    """Empty schedules list → zero fires, no errors."""
    fired = await scheduler.run_once(scheduler_env["state"])
    assert fired == 0
    assert len(scheduler_env["catcher"].messages) == 0


@pytest.mark.asyncio
async def test_run_once_fires_due_schedule(scheduler_env):
    """A schedule with last_run_epoch=None and a past cron slot
    fires, lands a message at the SMTP listener, and updates
    last_run_epoch on disk."""
    _add_schedule(scheduler_env["settings_path"], id="daily-fire")

    # Use a now_epoch that's after 08:00 UTC today for determinism.
    now_dt = datetime(2026, 4, 12, 9, 0, tzinfo=ZoneInfo("UTC"))
    now_epoch = int(now_dt.timestamp())

    fired = await scheduler.run_once(scheduler_env["state"], now_epoch=now_epoch)
    assert fired == 1

    # Message landed
    assert len(scheduler_env["catcher"].messages) == 1

    # last_run_epoch persisted
    data = json.loads(scheduler_env["settings_path"].read_text())
    assert data["schedules"][0]["last_run_epoch"] == now_epoch


@pytest.mark.asyncio
async def test_run_once_disabled_schedule_skipped(scheduler_env):
    """enabled=False → not fired."""
    _add_schedule(scheduler_env["settings_path"], enabled=False)

    now_epoch = int(datetime(2026, 4, 12, 9, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    fired = await scheduler.run_once(scheduler_env["state"], now_epoch=now_epoch)
    assert fired == 0
    assert len(scheduler_env["catcher"].messages) == 0


@pytest.mark.asyncio
async def test_run_once_empty_smtp_host_skips_all(scheduler_env):
    """smtp.host="" → all schedules skipped. This is the first-run
    state before the user has configured SMTP in the Settings view."""
    _add_schedule(scheduler_env["settings_path"])

    # Clobber the smtp config
    data = json.loads(scheduler_env["settings_path"].read_text())
    data["smtp"]["host"] = ""
    scheduler_env["settings_path"].write_text(json.dumps(data))

    now_epoch = int(datetime(2026, 4, 12, 9, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    fired = await scheduler.run_once(scheduler_env["state"], now_epoch=now_epoch)
    assert fired == 0
    assert len(scheduler_env["catcher"].messages) == 0


@pytest.mark.asyncio
async def test_run_once_no_recipients_skips_schedule(scheduler_env):
    """An enabled schedule with recipients=[] is skipped. Editing a
    schedule to drop all recipients shouldn't crash the loop."""
    _add_schedule(scheduler_env["settings_path"], recipients=[])

    now_epoch = int(datetime(2026, 4, 12, 9, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    fired = await scheduler.run_once(scheduler_env["state"], now_epoch=now_epoch)
    assert fired == 0
    assert len(scheduler_env["catcher"].messages) == 0


@pytest.mark.asyncio
async def test_run_once_already_fired_this_slot(scheduler_env):
    """A schedule with last_run_epoch > most-recent cron slot is
    NOT due. This catches the "don't fire the same 08:00 twice" rule."""
    # Set last_run_epoch to today at 08:30 — past 08:00's slot
    ran_at = int(datetime(2026, 4, 12, 8, 30, tzinfo=ZoneInfo("UTC")).timestamp())
    _add_schedule(scheduler_env["settings_path"], last_run_epoch=ran_at)

    # Tick at 09:00 — the most-recent slot (08:00) is before
    # last_run_epoch (08:30), so no fire.
    now_epoch = int(datetime(2026, 4, 12, 9, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    fired = await scheduler.run_once(scheduler_env["state"], now_epoch=now_epoch)
    assert fired == 0
