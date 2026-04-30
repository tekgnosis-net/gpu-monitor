"""NVML-based metric source.

Replaces the bash `nvidia-smi --query-gpu=...` subprocess invocation
with direct calls into `libnvidia-ml.so` via `pynvml`. Returns
structured `GPUMetric` instances; missing power telemetry surfaces as
`power_w=None` rather than the legacy `[N/A]` string sentinel.

The collector calls `NVMLSource.sample()` once per tick. Each
NVML query is fast (microseconds — no subprocess fork) so a
multi-GPU sample completes in well under a millisecond, even on the
slowest paths.

Per-GPU error isolation
-----------------------

We deliberately catch every `pynvml.NVMLError` per-metric and either
return None (omitting that GPU's reading entirely) or substitute
`None` for the affected field. The contract for callers:

  * Power: NVML_ERROR_NOT_SUPPORTED → `power_w=None` (→ SQL NULL),
    matching the v1.5.0 telemetry-gap contract. Any other NVMLError
    on power also surfaces as `power_w=None` with a WARNING log,
    rather than dropping the whole GPU's sample — a transient
    driver hiccup shouldn't lose temperature/util/memory readings
    we already collected.

  * Temperature / utilization / memory: any NVMLError on these
    metrics drops the GPU from the current tick (returns None from
    `_sample_one`) and logs a WARNING. The next tick retries with
    a fresh NVML call. NVML_ERROR_GPU_IS_LOST is the most common
    cause; the inventory will be re-discovered on next container
    restart.

`sample()` itself never raises. The collector loop relies on this
to avoid losing sibling-GPU readings when one GPU is in a degraded
state.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pynvml

from gpu_monitor.state import GPUInventory, GPUMetric

log = logging.getLogger("gpu-monitor.source")


class NVMLSource:
    """Cached-handle NVML sampler.

    Owns one `pynvml` device handle per GPU listed in the inventory it
    was constructed with. `sample()` returns the current per-GPU
    telemetry. Idempotent for repeated calls; no per-call init cost.
    """

    def __init__(self, inventories: list[GPUInventory]) -> None:
        self._inventories = inventories
        # Map index → (uuid, handle). Cached at construction so the
        # tight tick loop doesn't re-resolve handles on every call.
        self._handles: list[tuple[GPUInventory, Any]] = []
        for inv in inventories:
            try:
                handle = pynvml.nvmlDeviceGetHandleByIndex(inv.index)
            except pynvml.NVMLError as exc:
                log.warning(
                    "source: failed to acquire handle for GPU %d (%s); "
                    "skipping in tick loop",
                    inv.index, exc,
                )
                continue
            self._handles.append((inv, handle))

        # Prefer NVML's v2 memory API when available. The legacy v1
        # API computes `used = total - free`, which includes ~400 MiB
        # of driver-reserved memory (CUDA context, command queues,
        # etc.) that `nvidia-smi` excludes. The v2 API (added in
        # driver 525+) returns the user-visible "used" matching
        # `nvidia-smi --query-gpu=memory.used`. Detected once at
        # construction so the per-tick loop has no branch-mispredict
        # cost. If the driver doesn't support v2, the first attempt
        # to call it will raise and we'll downgrade the flag for the
        # rest of the process lifetime.
        self._mem_v2_available = hasattr(pynvml, "nvmlMemory_v2")

    def sample(self) -> list[GPUMetric]:
        """Sample every cached GPU. Returns one GPUMetric per GPU.

        A per-GPU error doesn't fail the whole sample — the rest of
        the GPUs still report. This matches the bash collector's
        per-row `[N/A]` tolerance, but with structured error codes
        instead of string parsing.
        """
        timestamp = int(time.time())
        out: list[GPUMetric] = []
        for inv, handle in self._handles:
            metric = self._sample_one(inv, handle, timestamp)
            if metric is not None:
                out.append(metric)
        return out

    def _sample_one(
        self,
        inv: GPUInventory,
        handle: Any,
        timestamp: int,
    ) -> GPUMetric | None:
        """Sample a single GPU. Returns None if the device is in a
        state where no useful reading can be produced (e.g. GPU_LOST)."""
        try:
            temperature = float(pynvml.nvmlDeviceGetTemperature(
                handle, pynvml.NVML_TEMPERATURE_GPU
            ))
        except pynvml.NVMLError as exc:
            log.warning("source: GPU %d temperature read failed (%s)", inv.index, exc)
            return None

        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            utilization = float(util.gpu)
        except pynvml.NVMLError as exc:
            log.warning("source: GPU %d utilization read failed (%s)", inv.index, exc)
            return None

        try:
            memory_mib = self._read_memory_used_mib(handle)
        except pynvml.NVMLError as exc:
            log.warning("source: GPU %d memory read failed (%s)", inv.index, exc)
            return None

        # Power: explicitly tolerate NOT_SUPPORTED → None (→ SQL NULL)
        # so 24h aggregations correctly exclude the gap rather than
        # averaging in a bogus 0.
        power_w: float | None
        try:
            power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)
            power_w = power_mw / 1000.0
        except pynvml.NVMLError as exc:
            err_code = getattr(exc, "value", None)
            if err_code == pynvml.NVML_ERROR_NOT_SUPPORTED:
                power_w = None
            else:
                log.warning(
                    "source: GPU %d power read failed (%s); recording NULL",
                    inv.index, exc,
                )
                power_w = None

        return GPUMetric(
            gpu_index=inv.index,
            gpu_uuid=inv.uuid,
            timestamp_epoch=timestamp,
            temperature=temperature,
            utilization=utilization,
            memory_mib=memory_mib,
            power_w=power_w,
        )

    def _read_memory_used_mib(self, handle: Any) -> float:
        """Return current GPU memory in use, in MiB, matching what
        `nvidia-smi --query-gpu=memory.used` would report.

        Tries the NVML v2 memory API first (driver 525+). If the
        driver/library doesn't support v2, downgrades the flag and
        falls back to v1 for the remaining process lifetime — the
        v1 result will overcount by ~400 MiB on Ampere/Ada cards
        but is still useful as a coarse trend indicator.
        """
        if self._mem_v2_available:
            try:
                mem = pynvml.nvmlDeviceGetMemoryInfo(
                    handle, version=pynvml.nvmlMemory_v2
                )
                return float(mem.used // (1024 * 1024))
            except (TypeError, pynvml.NVMLError) as exc:
                log.warning(
                    "source: NVML v2 memory API unavailable (%s); "
                    "falling back to v1 (will overcount used memory by "
                    "the driver-reserved chunk for the rest of this "
                    "process lifetime)",
                    exc,
                )
                self._mem_v2_available = False

        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return float(mem.used // (1024 * 1024))
