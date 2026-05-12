"""Tests for `gpu_monitor.inventory.refresh_power_limits`.

Refresh covers the case where an operator runs `nvidia-smi -pl <watts>`
against a running container — the enforced power limit changes at the
driver level but the JSON files were frozen at startup. These tests
verify:

  * No-op when limits match → no disk write (mtime stable)
  * Atomic rewrite when at least one limit changes → both files
    reflect the new value
  * Per-GPU NVML error isolation → failing GPU keeps old value,
    sibling GPUs still get refreshed
  * Empty / missing inventory.json → graceful no-op (no crash)
"""

from __future__ import annotations

import json
import os
import time

import pynvml

from gpu_monitor import inventory


# ─── Helpers ───────────────────────────────────────────────────────────────


def _seed_inventory(inv_path, cfg_path, gpus, *, version="2.1.1"):
    """Write a baseline inventory + config JSON that refresh_power_limits
    will read on entry."""
    payload_inv = {"gpus": gpus}
    payload_cfg = {
        "gpu_name": gpus[0]["name"] if gpus else "GPU",
        "version": version,
        "gpus": gpus,
    }
    inv_path.write_text(json.dumps(payload_inv, indent=2) + "\n")
    cfg_path.write_text(json.dumps(payload_cfg, indent=2) + "\n")


def _patch_limits(monkeypatch, limits_mw):
    """Wire pynvml so that GetHandleByIndex returns the index itself
    (handles are opaque in the real lib; we use ints as a stand-in)
    and GetEnforcedPowerLimit dispatches off the int index into
    `limits_mw`. A None entry triggers NVMLError."""
    monkeypatch.setattr(
        pynvml, "nvmlDeviceGetHandleByIndex", lambda i: i,
    )

    def _get_limit(handle):
        if handle >= len(limits_mw) or limits_mw[handle] is None:
            raise pynvml.NVMLError(pynvml.NVML_ERROR_NOT_SUPPORTED)
        return limits_mw[handle]

    monkeypatch.setattr(
        pynvml, "nvmlDeviceGetEnforcedPowerLimit", _get_limit,
    )


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_refresh_noop_when_limits_unchanged(tmp_path, monkeypatch):
    """Same limit reported by NVML → no rewrite, mtime preserved.
    This is the common case — most ticks don't change anything, so
    repeated calls must not churn the inventory file."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"
    _seed_inventory(inv_path, cfg_path, [
        {"index": 0, "uuid": "GPU-a", "name": "RTX 3090",
         "memory_total_mib": 24576, "power_limit_w": 220},
    ])
    # Backdate the file so we can detect any write reliably.
    old_mtime = time.time() - 3600
    os.utime(inv_path, (old_mtime, old_mtime))
    os.utime(cfg_path, (old_mtime, old_mtime))

    _patch_limits(monkeypatch, [220_000])  # 220 W matches existing

    changed, deltas = inventory.refresh_power_limits(
        inventory_path=inv_path, config_path=cfg_path, version="2.1.1",
    )

    assert changed is False
    assert deltas == []
    # mtime preserved → no rewrite
    assert inv_path.stat().st_mtime == old_mtime
    assert cfg_path.stat().st_mtime == old_mtime


def test_refresh_rewrites_on_limit_change(tmp_path, monkeypatch):
    """Operator runs `nvidia-smi -pl 250` → both files reflect the
    new 250 W limit; (idx, old, new) delta returned for logging."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"
    _seed_inventory(inv_path, cfg_path, [
        {"index": 0, "uuid": "GPU-a", "name": "RTX 3090",
         "memory_total_mib": 24576, "power_limit_w": 220},
    ])

    _patch_limits(monkeypatch, [250_000])  # 250 W — operator changed it

    changed, deltas = inventory.refresh_power_limits(
        inventory_path=inv_path, config_path=cfg_path, version="2.1.1",
    )

    assert changed is True
    assert deltas == [(0, 220, 250)]
    inv_data = json.loads(inv_path.read_text())
    assert inv_data["gpus"][0]["power_limit_w"] == 250
    cfg_data = json.loads(cfg_path.read_text())
    assert cfg_data["gpus"][0]["power_limit_w"] == 250
    # Static fields preserved across the rewrite
    assert inv_data["gpus"][0]["uuid"] == "GPU-a"
    assert inv_data["gpus"][0]["memory_total_mib"] == 24576


def test_refresh_per_gpu_error_isolation(tmp_path, monkeypatch):
    """If one GPU's NVML re-read raises (e.g. GPU_IS_LOST), that
    index keeps its old value while sibling GPUs still get the new
    value. The "any change → rewrite" semantics still hold via
    the surviving GPU's delta."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"
    _seed_inventory(inv_path, cfg_path, [
        {"index": 0, "uuid": "GPU-a", "name": "RTX 3090",
         "memory_total_mib": 24576, "power_limit_w": 220},
        {"index": 1, "uuid": "GPU-b", "name": "RTX 3090",
         "memory_total_mib": 24576, "power_limit_w": 220},
    ])

    # GPU 0 fails (None → NVMLError); GPU 1 reports 180 W.
    _patch_limits(monkeypatch, [None, 180_000])

    changed, deltas = inventory.refresh_power_limits(
        inventory_path=inv_path, config_path=cfg_path, version="2.1.1",
    )

    assert changed is True
    assert deltas == [(1, 220, 180)]
    inv_data = json.loads(inv_path.read_text())
    # GPU 0 kept old value (220), GPU 1 updated to 180
    assert inv_data["gpus"][0]["power_limit_w"] == 220
    assert inv_data["gpus"][1]["power_limit_w"] == 180


def test_refresh_handles_missing_inventory_file(tmp_path, monkeypatch):
    """If gpu_inventory.json is somehow missing (e.g. volume mount
    mid-flight, manual rm), refresh logs a warning and returns
    (False, []) — never raises into the housekeeping loop."""
    inv_path = tmp_path / "gpu_inventory.json"  # NOT created
    cfg_path = tmp_path / "gpu_config.json"

    # Even with NVML wired up, the missing file should short-circuit.
    _patch_limits(monkeypatch, [220_000])

    changed, deltas = inventory.refresh_power_limits(
        inventory_path=inv_path, config_path=cfg_path, version="2.1.1",
    )

    assert changed is False
    assert deltas == []
    # No phantom file got written
    assert not inv_path.exists()
    assert not cfg_path.exists()


def test_refresh_handles_empty_inventory(tmp_path, monkeypatch):
    """Synthetic empty-inventory edge case: {gpus: []} → no work to
    do, no rewrite, no crash."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"
    _seed_inventory(inv_path, cfg_path, [])  # writes gpus: []

    _patch_limits(monkeypatch, [])

    changed, deltas = inventory.refresh_power_limits(
        inventory_path=inv_path, config_path=cfg_path, version="2.1.1",
    )

    assert changed is False
    assert deltas == []
