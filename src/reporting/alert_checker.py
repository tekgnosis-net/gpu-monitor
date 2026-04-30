"""
Server-side GPU alert checker with push notification dispatch.

Runs as a supervised subprocess alongside the scheduler and web
server — same bash supervisor pattern (monitor_gpu.sh respawns
on crash, SIGTERM on docker stop).

Lifecycle:

    ┌────────────────────────────────────────────────┐
    │ monitor_gpu.sh  run_alert_checker()            │
    │   │                                             │
    │   while true                                    │
    │     python3 /app/reporting/alert_checker.py &   │
    │     wait $!                                     │
    │     log warning on exit                         │
    │     sleep 2                                     │
    │   done                                          │
    └────────────────────────────────────────────────┘

Main loop (default 30s cadence, configurable via poll_interval_seconds):

    1. Load settings.json → thresholds + channel configs
    2. Open SQLite read-only, query latest metric per GPU
    3. For each (gpu_index, metric): compare value vs threshold
    4. If over threshold AND not in cooldown → fire all enabled channels
    5. If below threshold → clear cooldown for that (gpu, metric) pair
    6. Sleep poll_interval_seconds and loop

State machine (per (gpu_index, metric_key)):

    idle  ─[value > threshold]─→  FIRE  ─[cooldown active]─→  suppressed
      ↑                                                            │
      └───────[value ≤ threshold, cooldown expired]────────────────┘

Cooldown is in-process memory (dict of last-fire timestamps). On
crash restart, the dict is empty → the first over-threshold tick
re-fires immediately. This is intentional: "if the process crashed
during a thermal event, re-notify when it comes back."

The module is importable so tests can drive run_once() directly
with a fixture DB and controlled timestamps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# Allow `from reporting import ...` when launched as a standalone
# script. Mirrors the scheduler.py trick.
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC_DIR = _SCRIPT_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from reporting import crypto, notifiers  # noqa: E402
from reporting.settings import load_settings  # noqa: E402


log = logging.getLogger("gpu-monitor.alert-checker")


# ─── Constants ─────────────────────────────────────────────────────────────

DEFAULT_BASE_DIR = Path("/app")
DEFAULT_TICK_SECONDS = 30

# Metric definitions: (settings_key, db_column, display_name, unit)
METRICS = [
    ("temperature_c", "temperature", "Temperature", "°C"),
    ("utilization_pct", "utilization", "Utilization", "%"),
    ("power_w", "power", "Power", "W"),
]


# ─── State ─────────────────────────────────────────────────────────────────


class _AlertCheckerState:
    """Encapsulates mutable running state so tests can drive run_once()
    without module-globals getting in the way."""

    def __init__(self, base_dir: Path = DEFAULT_BASE_DIR) -> None:
        self.base_dir = base_dir
        self.settings_file = base_dir / "history" / "settings.json"
        self.db_file = base_dir / "history" / "gpu_metrics.db"
        self.inventory_file = base_dir / "gpu_inventory.json"
        self.secret_key_file = base_dir / "history" / ".secret"
        self.stop_requested = False
        # Cooldown state: (gpu_index, metric_key) -> last_fire_epoch
        # In-process memory, not persisted. Resets on crash → immediate
        # re-fire on restart, which is the correct behavior.
        self.last_fire: dict[tuple[int, str], float] = {}


# ─── DB queries ────────────────────────────────────────────────────────────


def _open_db_readonly(db_file: Path) -> sqlite3.Connection:
    """Open SQLite in read-only WAL mode. Matches server.py's pattern."""
    uri = f"file:{db_file}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _get_latest_metrics(db_file: Path) -> list[dict[str, Any]]:
    """Fetch the most recent sample per GPU — same query as
    handle_metrics_current in server.py."""
    try:
        conn = _open_db_readonly(db_file)
    except sqlite3.OperationalError as exc:
        log.warning("alert_checker: cannot open DB: %s", exc)
        return []

    try:
        rows = conn.execute("""
            SELECT m.gpu_index, m.temperature, m.utilization, m.power
            FROM gpu_metrics m
            WHERE m.timestamp_epoch = (
                SELECT MAX(timestamp_epoch)
                FROM gpu_metrics m2
                WHERE m2.gpu_index = m.gpu_index
            )
            ORDER BY m.gpu_index ASC
        """).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        log.warning("alert_checker: query failed: %s", exc)
        return []
    finally:
        conn.close()


def _load_gpu_names(inventory_file: Path) -> dict[int, str]:
    """Load gpu_index → gpu_name mapping from gpu_inventory.json."""
    try:
        with open(inventory_file, "r") as f:
            inv = json.load(f)
        return {int(g["index"]): g.get("name", f"GPU {g['index']}")
                for g in inv.get("gpus", [])}
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return {}


# ─── Tick entry point ──────────────────────────────────────────────────────


async def run_once(
    state: _AlertCheckerState,
    now_epoch: float | None = None,
) -> int:
    """One alert checker tick. Loads settings, queries latest metrics,
    evaluates thresholds, fires notifications on breach.

    Returns the number of alerts fired in this tick (0+).

    Separated from the forever loop so tests can drive it directly
    with a fixture DB and controlled timestamps.
    """
    if now_epoch is None:
        now_epoch = time.time()

    settings_data = load_settings(state.settings_file)
    alerts_cfg = settings_data.get("alerts", {})
    channels_cfg = alerts_cfg.get("channels", {})
    cooldown_seconds = alerts_cfg.get("cooldown_seconds", 10)
    instance_name = alerts_cfg.get("instance_name", "")

    # Early exit: no channels enabled → nothing to do
    any_enabled = any(
        channels_cfg.get(ch, {}).get("enabled", False)
        for ch in ("ntfy", "pushover", "webhook", "email")
    )
    if not any_enabled:
        return 0

    metrics = _get_latest_metrics(state.db_file)
    if not metrics:
        return 0

    gpu_names = _load_gpu_names(state.inventory_file)

    # Load the encryption key for decrypting Pushover/webhook secrets
    try:
        secret_key = crypto.load_or_create_key(state.secret_key_file)
    except crypto.CryptoError as exc:
        log.error("alert_checker: cannot load encryption key: %s", exc)
        return 0

    smtp_config = settings_data.get("smtp", {})

    fired = 0

    for gpu_row in metrics:
        gpu_index = gpu_row.get("gpu_index", 0)
        gpu_name = gpu_names.get(gpu_index, f"GPU {gpu_index}")

        for threshold_key, db_column, display_name, unit in METRICS:
            threshold = alerts_cfg.get(threshold_key)
            if threshold is None:
                continue

            value = gpu_row.get(db_column)
            if value is None:
                continue

            key = (gpu_index, db_column)

            if value > threshold:
                # Over threshold — check cooldown
                last = state.last_fire.get(key)
                if last is not None and (now_epoch - last) < cooldown_seconds:
                    continue  # still in cooldown, suppress

                # FIRE
                state.last_fire[key] = now_epoch

                alert_data = notifiers.build_alert_data(
                    gpu_index=gpu_index,
                    gpu_name=gpu_name,
                    metric=display_name,
                    value=round(value, 1),
                    threshold=threshold,
                    unit=unit,
                )

                try:
                    succeeded = await notifiers.dispatch_alert(
                        channels_config=channels_cfg,
                        alert_data=alert_data,
                        smtp_config=smtp_config,
                        secret_key=secret_key,
                        instance_name=instance_name,
                    )
                    if succeeded:
                        log.info(
                            "alert_checker: GPU %d %s=%.1f > %.1f → fired %s",
                            gpu_index, db_column, value, threshold,
                            ", ".join(succeeded),
                        )
                        fired += 1
                    else:
                        log.warning(
                            "alert_checker: GPU %d %s=%.1f > %.1f → all channels failed",
                            gpu_index, db_column, value, threshold,
                        )
                except Exception as exc:
                    log.error(
                        "alert_checker: dispatch failed for GPU %d %s: %s",
                        gpu_index, db_column, exc,
                    )
            else:
                # Below threshold — clear cooldown so next breach
                # fires immediately without waiting for the cooldown
                # window to expire.
                state.last_fire.pop(key, None)

    return fired


# ─── Main forever loop ─────────────────────────────────────────────────────


def _install_signal_handlers(state: _AlertCheckerState) -> None:
    """Flip state.stop_requested on SIGTERM / SIGINT for clean shutdown."""
    def handler(signum, frame):  # pragma: no cover
        _ = signum, frame
        state.stop_requested = True
        log.info("alert_checker: stop signal received, exiting after current tick")

    try:
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):
        pass  # outside main thread — tolerated for test harnesses


async def main_loop(
    state: _AlertCheckerState,
    tick_seconds: int | None = None,
    install_signal_handlers: bool = True,
) -> None:
    """Forever loop: tick, sleep, tick, sleep. Exits cleanly when
    state.stop_requested is True (set by signal handler on SIGTERM).

    tick_seconds defaults to alerts.poll_interval_seconds from
    settings.json, re-read each tick so live changes take effect.

    `install_signal_handlers` defaults to True for backward compat
    with the legacy bash-supervised mode. The v2.0.0 unified
    entrypoint passes False so `gpu_monitor.lifecycle` owns
    SIGTERM/SIGINT — without this, `signal.signal(...)` here would
    override `loop.add_signal_handler(...)` set by lifecycle, and
    SIGTERM would never reach the supervisor's stop event.
    """
    if install_signal_handlers:
        _install_signal_handlers(state)
    log.info("alert_checker: started")

    while not state.stop_requested:
        # Re-read poll interval each tick so settings changes apply
        # without a process restart — same pattern as the collector's
        # live-reload of interval_seconds.
        effective_tick = tick_seconds
        if effective_tick is None:
            try:
                cfg = load_settings(state.settings_file)
                effective_tick = cfg.get("alerts", {}).get(
                    "poll_interval_seconds", DEFAULT_TICK_SECONDS)
            except Exception:
                effective_tick = DEFAULT_TICK_SECONDS

        try:
            fired = await run_once(state)
            if fired:
                log.info("alert_checker: fired %d alert(s) this tick", fired)
        except Exception as exc:  # pragma: no cover — last-resort guard
            log.exception("alert_checker: tick failed: %s", exc)

        # Sleep in 1s chunks for responsive SIGTERM handling
        for _ in range(effective_tick):
            if state.stop_requested:
                break
            await asyncio.sleep(1)

    log.info("alert_checker: exited")


def main() -> int:  # pragma: no cover
    """Entry point for bash supervisor launch."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    state = _AlertCheckerState()
    try:
        asyncio.run(main_loop(state))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
