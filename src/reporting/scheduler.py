"""
Standalone cron-driven report scheduler.

Phase 6b of the v1.0.0 overhaul. Runs as a supervised subprocess
alongside the aiohttp server and the bash collector — all three
share the same /app volume and talk through settings.json + the
SQLite DB, no in-process coupling.

Lifecycle:

    ┌────────────────────────────────────────────────┐
    │ monitor_gpu.sh  run_report_scheduler()         │
    │   │                                             │
    │   while true                                    │
    │     python3 /app/reporting/scheduler.py &       │
    │     wait $!                                     │
    │     log warning on exit                         │
    │     sleep 2                                     │
    │   done                                          │
    └────────────────────────────────────────────────┘

  Under normal operation the Python process runs forever and is
  reaped cleanly on SIGTERM during container shutdown. If it
  crashes (unhandled exception, disk full, etc.) the bash
  supervisor logs and respawns after a 2s back-off — same pattern
  as run_web_server.

Main loop (60s cadence):

    1. Load settings.json via load_settings() (defaults if missing)
    2. Filter schedules to enabled + past-due entries
    3. For each due schedule:
         a. decrypt smtp.password_enc via crypto.decrypt
         b. generate_report() via reporting.render
         c. send_message() via reporting.mailer
         d. save_settings() with updated last_run_epoch
         e. log success/failure
    4. sleep 60s and loop

A 60s wake is way coarser than cron's minute-level resolution,
but settings.json schedules are for daily/weekly/monthly reports
where a 60s drift is invisible.

Cron evaluation:

  We use croniter.get_prev() to find the most-recent "should have
  fired" time for each schedule. If that time is strictly after
  last_run_epoch, the schedule is due. This handles catch-up
  correctly: a container that was offline during the 8 AM firing
  wakes up, sees the most-recent 8 AM in the past, notices
  last_run_epoch is stale, and fires once on first tick. It does
  NOT fire multiple times for each missed day — only the most
  recent.

Empty / disabled SMTP config:

  If smtp.host is empty, the scheduler skips ALL schedules with a
  single WARN log at startup. This is the expected first-run
  state. As soon as the user configures SMTP via the Settings
  view, the next 60s tick picks up the new config and starts
  firing.

Failure handling:

  A render or send failure for one schedule MUST NOT kill the
  scheduler process. Each schedule is wrapped in its own
  try/except with error logging; the loop continues. An
  unrecoverable crash (KeyboardInterrupt, SystemExit) bubbles up
  to the bash supervisor which logs and respawns.

The module is importable so tests can drive the loop with
freezegun without launching a real subprocess.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Allow `from reporting import ...` when launched as a standalone
# script. Mirrors the server.py trick for the same reason — no
# setup.py in the image.
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from reporting import crypto, mailer, render  # noqa: E402
from reporting.settings import (  # noqa: E402
    load_settings,
    save_settings,
)


log = logging.getLogger("gpu-monitor.scheduler")


# ─── Constants ─────────────────────────────────────────────────────────────

DEFAULT_BASE_DIR = Path("/app")
DEFAULT_TICK_SECONDS = 60
DEFAULT_STOP = False  # flipped by signal handler to request graceful exit


def _resolve_tz() -> ZoneInfo:
    """Return a ZoneInfo object honoring the TZ environment variable,
    falling back to UTC if the zone isn't available in the container.

    The python:3.11-slim base image ships without `tzdata`, so a
    user who runs with -e TZ=America/Los_Angeles would otherwise
    crash the scheduler at startup with ZoneInfoNotFoundError. The
    bash supervisor would respawn the subprocess, the subprocess
    would crash again on the same line, and we'd be stuck in an
    infinite respawn loop burning container resources. UTC is
    always available because Python's zoneinfo module has a
    hardcoded UTC fallback.

    We log a warning once so the user knows why their configured
    TZ isn't being honored — silently ignoring the setting would
    make cron schedules fire at "wrong" times with no explanation.
    """
    requested = os.environ.get("TZ", "UTC")
    try:
        return ZoneInfo(requested)
    except ZoneInfoNotFoundError:
        log.warning(
            "scheduler: TZ=%s not found (tzdata may be missing in this image); "
            "falling back to UTC. Install tzdata in the Dockerfile to honor "
            "local time zones.",
            requested,
        )
        return ZoneInfo("UTC")


class _SchedulerState:
    """Encapsulates the mutable running state so the main loop can
    be driven from tests without module-globals getting in the way."""

    def __init__(self, base_dir: Path = DEFAULT_BASE_DIR) -> None:
        self.base_dir = base_dir
        self.settings_file = base_dir / "settings.json"
        self.db_file = base_dir / "history" / "gpu_metrics.db"
        self.inventory_file = base_dir / "gpu_inventory.json"
        self.version_file = base_dir / "VERSION"
        self.secret_key_file = base_dir / "history" / ".secret"
        self.tz = _resolve_tz()
        self.stop_requested = False


# ─── Cron evaluation ───────────────────────────────────────────────────────


def _is_due(cron_expr: str, last_run_epoch: int | None, now_epoch: int,
            tz: ZoneInfo) -> bool:
    """Return True if the schedule should fire on this tick.

    Strategy: use croniter.get_prev() to find the most-recent
    scheduled time <= now. If that time is strictly after
    last_run_epoch (or last_run_epoch is None / 0), we're due.

    This catches up missed fires without firing multiple times per
    backlog: a container offline for 3 days comes up, sees the
    most-recent schedule time in the past (yesterday 8 AM), fires
    once, updates last_run_epoch. Tomorrow 8 AM triggers again.
    The 6 AM two days ago is NOT re-fired — only the most recent
    missed slot counts.

    Importing croniter is lazy inside this helper so tests that
    don't exercise the cron path don't pay the import cost. It's
    a small pure-Python module but there's no need to load it at
    module import.
    """
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover — croniter is a runtime dep
        log.error("croniter not installed: %s", exc)
        return False

    try:
        now_dt = datetime.fromtimestamp(now_epoch, tz=tz)
        cron = croniter(cron_expr, now_dt)
        prev_dt = cron.get_prev(datetime)
    except (ValueError, KeyError) as exc:
        log.warning("invalid cron expression %r: %s", cron_expr, exc)
        return False

    prev_epoch = int(prev_dt.timestamp())

    # Never-run schedule with a valid cron → fire on first tick
    # that has a past "most recent" time. An empty settings.json
    # with a freshly-added schedule has last_run_epoch=None; the
    # first tick will fire.
    if not last_run_epoch:
        return prev_epoch > 0

    return prev_epoch > int(last_run_epoch)


# ─── Send path ─────────────────────────────────────────────────────────────


async def _fire_schedule(
    state: _SchedulerState,
    settings_data: dict,
    schedule: dict,
    version: str,
) -> bool:
    """Render + send one scheduled report. Returns True on success.

    Not `raise`-ing on failure is deliberate: the loop needs to
    continue even if one schedule has a broken cron or a dead
    SMTP relay. Errors are logged with the schedule id so
    operators can grep for them.
    """
    schedule_id = schedule.get("id", "?")
    template = schedule.get("template", "daily")
    custom_subject = schedule.get("subject") or None
    recipients = schedule.get("recipients") or []
    if not recipients:
        log.warning("scheduler: schedule %s has no recipients, skipping",
                    schedule_id)
        return False

    smtp = settings_data.get("smtp", {})
    if not smtp.get("host"):
        log.info("scheduler: SMTP not configured, skipping schedule %s",
                 schedule_id)
        return False

    # Decrypt the SMTP password — empty ciphertext passes through as
    # empty plaintext (matches the mailer's "anonymous relay" path).
    try:
        key = crypto.load_or_create_key(state.secret_key_file)
        plaintext = crypto.decrypt(smtp.get("password_enc", ""), key)
    except crypto.CryptoError as exc:
        log.error("scheduler: cannot decrypt SMTP password: %s", exc)
        return False

    # Render the report — matplotlib + Jinja + premailer
    try:
        message = render.generate_report(
            template=template,
            db_file=state.db_file,
            inventory_file=state.inventory_file,
            settings_file=state.settings_file,
            version=version,
            subject_override=custom_subject,
        )
    except render.RenderError as exc:
        log.error("scheduler: render failed for schedule %s: %s",
                  schedule_id, exc)
        return False
    except Exception as exc:  # pragma: no cover — matplotlib surprises
        log.error("scheduler: unexpected render failure for %s: %s",
                  schedule_id, exc)
        return False

    # Stamp From / To from the resolved config + schedule
    from_addr = smtp.get("from") or smtp.get("user") or "gpu-monitor@localhost"
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)

    # Send
    try:
        await mailer.send_message(
            message,
            host=smtp.get("host", ""),
            port=int(smtp.get("port") or 587),
            user=smtp.get("user", "") or "",
            password=plaintext,
            tls=smtp.get("tls") or "starttls",
        )
    except mailer.MailerError as exc:
        log.error("scheduler: send failed for schedule %s: %s",
                  schedule_id, exc)
        return False

    log.info("scheduler: sent schedule %s (%s → %s)",
             schedule_id, template, ", ".join(recipients))
    return True


# ─── Tick entry point ──────────────────────────────────────────────────────


async def run_once(state: _SchedulerState, now_epoch: int | None = None) -> int:
    """One scheduler tick. Loads settings, evaluates due schedules,
    fires them, updates last_run_epoch. Returns the number of
    schedules fired in this tick (0+).

    Separated from the forever loop so tests can drive it directly
    with a controlled `now_epoch` and a fake settings file.
    """
    if now_epoch is None:
        now_epoch = int(time.time())

    settings_data = load_settings(state.settings_file)
    schedules = settings_data.get("schedules") or []
    if not schedules:
        return 0

    try:
        version = state.version_file.read_text().strip() or "unknown"
    except OSError:
        version = "unknown"

    fired = 0
    # We iterate over indices so we can update last_run_epoch on
    # the original list and save_settings at the end atomically.
    for index, schedule in enumerate(schedules):
        if not isinstance(schedule, dict):
            continue
        if not schedule.get("enabled", True):
            continue

        cron_expr = schedule.get("cron")
        if not isinstance(cron_expr, str) or not cron_expr:
            continue

        if not _is_due(cron_expr, schedule.get("last_run_epoch"), now_epoch,
                       state.tz):
            continue

        ok = await _fire_schedule(state, settings_data, schedule, version)
        if ok:
            # Write the fire time into the in-memory structure so
            # the subsequent save_settings persists it. We stamp
            # now_epoch rather than the "should have fired" time
            # so the next tick's is_due() calculation treats this
            # fire as having happened at the actual wall-clock
            # moment of send.
            schedules[index]["last_run_epoch"] = now_epoch
            fired += 1

    if fired > 0:
        # CONCURRENCY: Reload the latest settings right before
        # persisting, then patch ONLY the last_run_epoch fields
        # of schedules we actually fired. This prevents the
        # lost-update pattern where a user PUT /api/settings
        # during our render+send window gets clobbered by our
        # stale settings_data snapshot.
        #
        # The `fired_timestamps` dict records (schedule_id →
        # now_epoch) pairs; we look up each fired id in the
        # reloaded schedules list and stamp the matching entry.
        # Schedules added/removed/edited by a concurrent writer
        # between our initial load and now are preserved
        # exactly — we only touch last_run_epoch on the ids we
        # own.
        fired_ids = {
            s["id"]: schedules[i]["last_run_epoch"]
            for i, s in enumerate(schedules)
            if isinstance(s, dict)
            and s.get("last_run_epoch") == now_epoch
            and s.get("id")
        }
        try:
            latest = load_settings(state.settings_file)
            latest_schedules = list(latest.get("schedules") or [])
            for s in latest_schedules:
                if isinstance(s, dict) and s.get("id") in fired_ids:
                    s["last_run_epoch"] = fired_ids[s["id"]]
            latest["schedules"] = latest_schedules
            save_settings(state.settings_file, latest)
        except OSError as exc:
            log.error("scheduler: could not persist last_run_epoch: %s", exc)

    return fired


# ─── Main forever loop ─────────────────────────────────────────────────────


def _install_signal_handlers(state: _SchedulerState) -> None:
    """Flip state.stop_requested on SIGTERM / SIGINT so the current
    tick finishes cleanly before the loop exits. SIGTERM is what
    Docker sends on `docker stop`; SIGINT is what Ctrl+C sends
    in an interactive shell."""
    def handler(signum, frame):  # pragma: no cover — signal delivery
        _ = signum, frame
        state.stop_requested = True
        log.info("scheduler: stop signal received, exiting after current tick")

    try:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        # signal() fails outside the main thread — we tolerate
        # that for test harnesses that call main() from a worker
        # thread. The forever loop is run from __main__ in
        # production so the handlers land correctly there.
        pass


async def main_loop(state: _SchedulerState,
                    tick_seconds: int = DEFAULT_TICK_SECONDS) -> None:
    """Forever loop: tick, sleep, tick, sleep. Exits cleanly when
    state.stop_requested is True (set by the signal handler on
    SIGTERM)."""
    import asyncio
    _install_signal_handlers(state)
    log.info("scheduler: started, tick=%ds, tz=%s", tick_seconds, state.tz)
    while not state.stop_requested:
        try:
            fired = await run_once(state)
            if fired:
                log.info("scheduler: fired %d schedule(s) this tick", fired)
        except Exception as exc:  # pragma: no cover — last-resort guard
            log.exception("scheduler: tick failed: %s", exc)
        # Sleep in small chunks so stop_requested is honored within
        # ~1 second of signal delivery rather than making the
        # container shutdown wait a full tick_seconds.
        for _ in range(tick_seconds):
            if state.stop_requested:
                break
            await asyncio.sleep(1)
    log.info("scheduler: exited")


def main() -> int:  # pragma: no cover — invoked only by __main__
    """Entry point when the module is launched as a script by the
    bash supervisor. Sets up logging and hands off to main_loop.
    Returns a POSIX exit code for the bash while-loop to log."""
    import asyncio

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    state = _SchedulerState()

    try:
        asyncio.run(main_loop(state))
        return 0
    except KeyboardInterrupt:
        return 130  # POSIX Ctrl+C exit code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
