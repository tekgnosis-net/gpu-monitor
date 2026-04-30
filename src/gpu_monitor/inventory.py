"""GPU inventory discovery via NVML.

Replaces the bash `discover_gpus()` function. Writes
`/app/gpu_inventory.json` (consumed by server.py /api/gpus and the
collector at runtime) and `/app/gpu_config.json` (consumed by the
frontend).

NVML returns the *currently enforced* power cap via
`nvmlDeviceGetEnforcedPowerLimit`, which is exactly the
`power.limit` semantic we want (and which the bash collector was
incorrectly fetching as `power.max_limit` until the v1.5.0 fix).

Output JSON shape — byte-compatible with the legacy bash output so
server.py and the frontend can read it unchanged:

    gpu_inventory.json:
        {"gpus": [{"index": 0, "uuid": "GPU-…", "name": "…",
                   "memory_total_mib": 24576, "power_limit_w": 220},
                  …]}

    gpu_config.json:
        {"gpu_name": "<first GPU name>",
         "version": "<gpu_monitor.__version__>",
         "gpus": [<same list as inventory>]}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pynvml

from gpu_monitor.state import GPUInventory

log = logging.getLogger("gpu-monitor.inventory")


def discover(
    *,
    inventory_path: str | Path,
    config_path: str | Path,
    version: str,
) -> list[GPUInventory]:
    """Enumerate attached GPUs via NVML and write both legacy JSON
    files. Returns the list of GPUInventory entries (also useful to
    pass directly into the collector source).

    Assumes `pynvml.nvmlInit()` has already been called (typically by
    `gpu_monitor.__main__`). On installs with no GPUs attached or NVML
    reporting a count of zero, falls back to a synthetic single-GPU
    inventory (`uuid='legacy-unknown'`) — same behavior as the bash
    `discover_gpus()` did, to keep the dashboard rendering even on
    partial installs.
    """
    inventories: list[GPUInventory] = []
    try:
        count = pynvml.nvmlDeviceGetCount()
    except pynvml.NVMLError as exc:
        log.warning("inventory: nvmlDeviceGetCount failed: %s", exc)
        count = 0

    for i in range(count):
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            inv = _inventory_from_handle(i, handle)
            inventories.append(inv)
        except pynvml.NVMLError as exc:
            log.warning("inventory: skipping GPU %d (%s)", i, exc)

    if not inventories:
        log.warning(
            "inventory: NVML reported no GPUs; falling back to synthetic "
            "single-GPU inventory (uuid='legacy-unknown')"
        )
        inventories.append(GPUInventory(
            index=0,
            uuid="legacy-unknown",
            name="GPU",
            memory_total_mib=0,
            power_limit_w=0,
        ))

    _write_inventory_json(inventory_path, inventories)
    _write_config_json(config_path, inventories, version=version)

    log.info(
        "inventory: detected %d GPU(s): %s",
        len(inventories),
        ", ".join(f"{inv.index}={inv.name}" for inv in inventories),
    )
    return inventories


# ─── NVML reads ────────────────────────────────────────────────────────────


def _inventory_from_handle(index: int, handle: Any) -> GPUInventory:
    """Build a GPUInventory from an NVML device handle.

    NVML returns:
      * UUID + name as either bytes (older nvidia-ml-py) or str
        (newer); we normalize to str.
      * memory.total in bytes; we convert to MiB to match the legacy
        `nvidia-smi --units=MiB` output.
      * power.limit in milliwatts via `nvmlDeviceGetEnforcedPowerLimit`,
        which corresponds to the *currently enforced* cap (the same
        value `nvidia-smi --query-gpu=power.limit` returns). Converted
        to whole watts.
    """
    uuid = _to_str(pynvml.nvmlDeviceGetUUID(handle))
    name = _to_str(pynvml.nvmlDeviceGetName(handle))

    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
    memory_total_mib = int(mem.total // (1024 * 1024))

    try:
        power_limit_mw = pynvml.nvmlDeviceGetEnforcedPowerLimit(handle)
        power_limit_w = int(round(power_limit_mw / 1000.0))
    except pynvml.NVMLError as exc:
        log.warning(
            "inventory: GPU %d power limit unavailable (%s); recording 0",
            index, exc,
        )
        power_limit_w = 0

    return GPUInventory(
        index=index,
        uuid=uuid,
        name=name,
        memory_total_mib=memory_total_mib,
        power_limit_w=power_limit_w,
    )


def _to_str(value: bytes | str) -> str:
    """Normalize NVML's bytes-or-str return into str. Older
    nvidia-ml-py releases returned bytes that callers had to decode;
    newer ones return str directly. We accept both for forward
    compatibility."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# ─── JSON writers (atomic) ─────────────────────────────────────────────────


def _write_inventory_json(
    path: str | Path,
    inventories: list[GPUInventory],
) -> None:
    """Write gpu_inventory.json in the legacy format. Atomic via
    write-to-tmp + rename so a partial write never leaves the dashboard
    reading a half-written file."""
    payload = {
        "gpus": [
            {
                "index": inv.index,
                "uuid": inv.uuid,
                "name": inv.name,
                "memory_total_mib": inv.memory_total_mib,
                "power_limit_w": inv.power_limit_w,
            }
            for inv in inventories
        ]
    }
    _atomic_write_json(path, payload)


def _write_config_json(
    path: str | Path,
    inventories: list[GPUInventory],
    *,
    version: str,
) -> None:
    """Write gpu_config.json in the legacy format. The top-level
    `gpu_name` key is the first GPU's name (for legacy single-GPU
    frontend code that pre-dates the multi-GPU rewrite)."""
    payload = {
        "gpu_name": inventories[0].name if inventories else "GPU",
        "version": version,
        "gpus": [
            {
                "index": inv.index,
                "uuid": inv.uuid,
                "name": inv.name,
                "memory_total_mib": inv.memory_total_mib,
                "power_limit_w": inv.power_limit_w,
            }
            for inv in inventories
        ],
    }
    _atomic_write_json(path, payload)


def _atomic_write_json(path: str | Path, payload: dict) -> None:
    """Write JSON to path.tmp then rename to path. Mirrors the legacy
    bash `safe_write_json` pattern."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)
