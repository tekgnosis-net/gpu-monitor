"""Typed dataclasses shared across the collector pipeline."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GPUInventory:
    """Static, per-boot facts about a GPU.

    Discovered once at startup via NVML and written to
    `/app/gpu_inventory.json` for `server.py` to surface via /api/gpus.
    """

    index: int
    uuid: str
    name: str
    memory_total_mib: int
    power_limit_w: int


@dataclass(frozen=True, slots=True)
class GPUMetric:
    """One sample of GPU telemetry.

    `power_w` is `None` (→ SQL NULL) when NVML reports
    `NVML_ERROR_NOT_SUPPORTED` for `nvmlDeviceGetPowerUsage`. This
    matches the v1.5.0 contract introduced when we patched the bash
    collector's `[N/A]` → 0 conflation, but enforced via the type
    system rather than a string sentinel.
    """

    gpu_index: int
    gpu_uuid: str
    timestamp_epoch: int
    temperature: float
    utilization: float
    memory_mib: float
    power_w: float | None
