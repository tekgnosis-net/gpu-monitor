"""Entry point for the gpu_monitor package.

Run via `python3 -m gpu_monitor`. Replaces `./monitor_gpu.sh` as the
container CMD.

Lifecycle:
    1. Configure logging (levels + format compatible with the legacy
       bash collector's "[YYYY-MM-DD HH:MM:SS] LEVEL: ..." style).
    2. pynvml.nvmlInit() — fail-fast if NVML unavailable.
    3. inventory.discover() → write gpu_inventory.json + gpu_config.json.
    4. db.migrate() then db.initialize() — schema is current.
    5. asyncio.gather:
         - collector.run()       (NVML sample → SQLite INSERT)
         - server task           (aiohttp /api/* + static)
         - scheduler.main_loop() (existing reporting/scheduler)
         - alert_checker.main_loop() (existing reporting/alert_checker)
         - housekeeping.run()    (log rotation + DB purge)
    6. On SIGTERM: lifecycle.supervise() cancels tasks + nvmlShutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import WatchedFileHandler
from pathlib import Path

import pynvml
from aiohttp import web

# server.py + reporting/* live in /app at the same level as gpu_monitor/,
# so importing them directly works as long as PYTHONPATH includes /app.
import server as server_module  # noqa: E402
from reporting import alert_checker, scheduler  # noqa: E402

from gpu_monitor import __version__, collector, db, housekeeping, inventory, lifecycle
from gpu_monitor.source import NVMLSource


log = logging.getLogger("gpu-monitor")


# ─── Path resolution ───────────────────────────────────────────────────────


def _path(env_var: str, default: str | Path) -> Path:
    """Resolve a path from an env var with a fallback default. Lets
    tests override paths without monkey-patching constants."""
    return Path(os.environ.get(env_var, default))


def _safe_int_env(env_var: str, default: int) -> int:
    """Parse an integer env var, falling back silently on bad input
    so a typo in a compose file doesn't crash the container at
    import time. The fallback is logged later (after _configure_logging
    runs) via _emit_deferred_warnings()."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _DEFERRED_WARNINGS.append(
            f"{env_var}={raw!r} is not an integer; falling back to {default}"
        )
        return default


_DEFERRED_WARNINGS: list[str] = []

BASE_DIR = _path("GPU_MONITOR_BASE", "/app")
DB_FILE = _path("GPU_MONITOR_DB", BASE_DIR / "history" / "gpu_metrics.db")
SETTINGS_FILE = _path("GPU_MONITOR_SETTINGS", BASE_DIR / "history" / "settings.json")
LOG_DIR = _path("GPU_MONITOR_LOG_DIR", BASE_DIR / "logs")
INVENTORY_FILE = _path("GPU_MONITOR_INVENTORY", BASE_DIR / "gpu_inventory.json")
CONFIG_FILE = _path("GPU_MONITOR_CONFIG", BASE_DIR / "gpu_config.json")
VERSION_FILE = _path("GPU_MONITOR_VERSION_FILE", BASE_DIR / "VERSION")
WEB_PORT = _safe_int_env("GPU_MONITOR_PORT", 8081)


def _read_version() -> str:
    """Single source of truth for the running version: /app/VERSION,
    populated at build time from the APP_VERSION build-arg. Falls
    back to the package's hardcoded `__version__` constant if the
    file is missing (typical only in dev-mode `python3 -m gpu_monitor`
    runs from a checkout). Avoids the drift Copilot flagged where the
    /api/version route reads /app/VERSION but the startup banner +
    gpu_config.json could disagree."""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
        return text or __version__
    except OSError:
        return __version__


VERSION = _read_version()


# ─── Logging ───────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    """Single combined log file at logs/app.log, plus stdout. Format
    matches the legacy bash output enough that container-log scrapers
    don't need to be retrained."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Drop any pre-existing handlers (e.g., from an embedded test harness)
    for h in list(root.handlers):
        root.removeHandler(h)

    # WatchedFileHandler (not plain FileHandler) is required because
    # housekeeping.py rotates by rename + touch. A plain FileHandler
    # would keep the file descriptor pointing at the old (renamed)
    # inode and continue writing into the rotated file forever,
    # defeating the size cap. WatchedFileHandler stat()s the path on
    # every emit and reopens on inode change. The cost is negligible
    # (one stat per log line).
    file_handler = WatchedFileHandler(LOG_DIR / "app.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Stdout handler so `docker compose logs` still works.
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    # Emit any warnings buffered during module import (e.g., a
    # malformed GPU_MONITOR_PORT). These need to wait until logging
    # is configured because they happen before this function runs.
    for msg in _DEFERRED_WARNINGS:
        log.warning(msg)
    _DEFERRED_WARNINGS.clear()


# ─── server task ───────────────────────────────────────────────────────────


async def _run_server() -> None:
    """Run the aiohttp app via AppRunner + TCPSite so it composes
    cleanly with asyncio.gather (web.run_app would block its own
    event loop). Cancellation cleanly tears down the runner."""
    app = server_module.make_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=WEB_PORT)
    await site.start()
    log.info("server: listening on http://0.0.0.0:%d", WEB_PORT)
    try:
        # Block forever (until cancellation)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        log.info("server: stopped")


# ─── reporting wrappers ────────────────────────────────────────────────────


async def _run_scheduler() -> None:
    """Run the existing reporting.scheduler main_loop. Pass
    install_signal_handlers=False so lifecycle.supervise owns
    SIGTERM/SIGINT — without this, signal.signal(...) inside
    main_loop would override loop.add_signal_handler(...) set by
    lifecycle, and SIGTERM would never reach the supervisor's
    stop event (hanging `docker stop`)."""
    state = scheduler._SchedulerState()
    await scheduler.main_loop(state, install_signal_handlers=False)


async def _run_alert_checker() -> None:
    """Same rationale as _run_scheduler above."""
    state = alert_checker._AlertCheckerState()
    await alert_checker.main_loop(state, install_signal_handlers=False)


# ─── main ──────────────────────────────────────────────────────────────────


async def _async_main() -> None:
    """Spawn all five async tasks under the lifecycle supervisor."""
    # Build the NVML source from the discovered inventory. Done once
    # at startup; hot-add/remove of GPUs requires a container restart
    # (matches legacy behavior).
    inventories = inventory.discover(
        inventory_path=INVENTORY_FILE,
        config_path=CONFIG_FILE,
        version=VERSION,
    )
    source = NVMLSource(inventories)

    await lifecycle.supervise([
        lambda: collector.run(
            source=source, db_path=DB_FILE, settings_path=SETTINGS_FILE,
        ),
        lambda: _run_server(),
        lambda: _run_scheduler(),
        lambda: _run_alert_checker(),
        lambda: housekeeping.run(
            log_dir=LOG_DIR, db_path=DB_FILE, settings_path=SETTINGS_FILE,
        ),
    ])


def main() -> int:
    _configure_logging()

    log.info("=" * 40)
    log.info("Starting NVIDIA GPU Monitor v%s", VERSION)
    log.info("https://github.com/tekgnosis-net/gpu-monitor")
    log.info("-" * 40)

    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as exc:
        log.error(
            "NVML initialization failed (%s). Is the NVIDIA driver "
            "loaded and `libnvidia-ml.so` present in the container? "
            "v2.0.0 has no nvidia-smi subprocess fallback by design — "
            "verify that the NVIDIA Container Toolkit is configured "
            "and the container has GPU access (e.g. `--gpus all`).",
            exc,
        )
        return 1

    try:
        # Schema migration runs ONCE before any reader hits the DB.
        # If pynvml gave us at least one inventory, use the first
        # GPU's UUID as the backfill value for any pre-existing rows
        # with NULL gpu_uuid (matches legacy behavior).
        try:
            uuid_for_backfill = pynvml.nvmlDeviceGetUUID(
                pynvml.nvmlDeviceGetHandleByIndex(0)
            )
            if isinstance(uuid_for_backfill, bytes):
                uuid_for_backfill = uuid_for_backfill.decode("utf-8", "replace")
        except pynvml.NVMLError:
            uuid_for_backfill = "legacy-unknown"

        db.migrate(DB_FILE, current_uuid=uuid_for_backfill)
        db.initialize(DB_FILE)

        log.info("Database ready at %s", DB_FILE)
        log.info("=" * 40)

        try:
            asyncio.run(_async_main())
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — exiting")
            return 130
        return 0
    finally:
        lifecycle.shutdown_nvml()


if __name__ == "__main__":
    raise SystemExit(main())
