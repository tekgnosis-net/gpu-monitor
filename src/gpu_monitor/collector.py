"""Async GPU metric collector loop.

Replaces the bash `update_stats()` tick + `process_buffer()` flush.
Direct per-tick INSERT under SQLite WAL — no buffer staging, no
retry ladder, no audit log (see plan rationale: WAL contention is
sub-millisecond and almost never fails; the batch-flush loss model
that motivated the bash machinery is gone).

The loop:

  1. Re-read `interval_seconds` from settings.json if its mtime
     changed since last tick (cheap stat, only re-parse on change).
  2. Sample the NVMLSource — cached handles, no subprocess fork.
  3. INSERT the samples into gpu_metrics.
  4. asyncio.sleep(interval) until the next tick.

A failure in step 2 or 3 is logged at WARNING with sample count and
the loop continues. We do NOT retry: each tick is its own
transaction, so a single dropped sample is a strictly better failure
mode than the legacy 30-sample-batch flush retry that motivated the
bash `.pending`/`.stuck-*` machinery.

The legacy `flush_interval_seconds` setting field is read tolerantly
for backward compat (existing settings.json files have it) but
ignored — direct per-tick writes have no batch cadence to tune.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from gpu_monitor import db
from gpu_monitor.source import NVMLSource

log = logging.getLogger("gpu-monitor.collector")


# Tick cadence bounds — matches load_settings() validation in monitor_gpu.sh.
DEFAULT_INTERVAL_S = 4
MIN_INTERVAL_S = 2
MAX_INTERVAL_S = 300


class _SettingsLoader:
    """Mtime-cached reader for `settings.json`.

    Only re-parses the file when its mtime changes — a stat() per
    tick is microseconds, while a JSON parse on a multi-KB file is
    measurable. Returns the current `collection.interval_seconds`
    value, falling back to DEFAULT_INTERVAL_S on any read/parse/
    validation error.
    """

    def __init__(self, settings_path: str | Path) -> None:
        self._path = Path(settings_path)
        self._cached_mtime: float | None = None
        self._cached_interval: int = DEFAULT_INTERVAL_S

    def current_interval(self) -> int:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            # File doesn't exist (fresh install) — default and don't
            # spam logs about it; load_settings() in bash also tolerated
            # a missing file silently.
            return DEFAULT_INTERVAL_S

        if mtime == self._cached_mtime:
            return self._cached_interval

        new_interval = self._read_interval()
        if new_interval != self._cached_interval:
            log.warning(
                "Collection settings reloaded: interval=%ds (was %ds)",
                new_interval, self._cached_interval,
            )
        self._cached_mtime = mtime
        self._cached_interval = new_interval
        return new_interval

    def _read_interval(self) -> int:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "settings: cannot read %s (%s); using default interval %ds",
                self._path, exc, DEFAULT_INTERVAL_S,
            )
            return DEFAULT_INTERVAL_S

        raw = data.get("collection", {}).get("interval_seconds", DEFAULT_INTERVAL_S)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_INTERVAL_S
        if value < MIN_INTERVAL_S or value > MAX_INTERVAL_S:
            log.warning(
                "settings: interval_seconds=%r out of range [%d, %d]; "
                "clamping to default %d",
                raw, MIN_INTERVAL_S, MAX_INTERVAL_S, DEFAULT_INTERVAL_S,
            )
            return DEFAULT_INTERVAL_S
        return value


async def run(
    *,
    source: NVMLSource,
    db_path: str | Path,
    settings_path: str | Path,
) -> None:
    """Run the collector tick loop until cancelled.

    Cancellation propagates as `asyncio.CancelledError` from the
    sleep — the supervisor (lifecycle.py) catches it and proceeds
    with shutdown. We deliberately do not swallow the cancellation
    here.
    """
    settings = _SettingsLoader(settings_path)
    log.info("collector: started")
    try:
        while True:
            interval = settings.current_interval()
            try:
                samples = source.sample()
            except Exception:
                log.exception("collector: NVML sample failed; skipping tick")
                samples = []

            if samples:
                try:
                    db.insert_samples(db_path, samples, interval_s=interval)
                except Exception:
                    # Log the GPU indexes that were dropped so an
                    # operator can correlate with timestamps if a
                    # gap appears in the dashboard.
                    indexes = [s.gpu_index for s in samples]
                    log.exception(
                        "collector: SQLite insert failed for GPUs %s; "
                        "dropping %d sample(s)", indexes, len(samples),
                    )

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        log.info("collector: stopped")
        raise
