"""Tests for `gpu_monitor.inventory`.

The discovery path runs against pynvml mocks because CI machines have
no GPUs. The tests verify:

  * The output JSON shape matches what server.py /api/gpus and the
    frontend expect (regression guard against breaking the dashboard).
  * Both bytes and str returns from older/newer pynvml are normalized.
  * `nvmlDeviceGetEnforcedPowerLimit` failure (NVML_ERROR_NOT_SUPPORTED)
    falls back to power_limit_w=0 with a warning, not a hard fail.
  * Empty inventory falls back to the synthetic single-GPU stub
    (uuid='legacy-unknown') matching the bash behavior.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pynvml

from gpu_monitor import inventory


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _make_handle(*, uuid, name, mem_total_bytes, power_limit_mw):
    """Build a mock NVML device handle with attached return values."""
    return {
        "uuid": uuid,
        "name": name,
        "mem_total_bytes": mem_total_bytes,
        "power_limit_mw": power_limit_mw,
    }


def _patch_nvml(monkeypatch, handles):
    """Wire up a fake pynvml such that nvmlDeviceGetCount returns
    len(handles), and the per-handle accessors return whatever the
    handle dict carries. Each call validates the (handle dict identity,
    method) pair so a wrong handle wired to a wrong method would fail."""
    monkeypatch.setattr(pynvml, "nvmlDeviceGetCount", lambda: len(handles))
    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex", lambda i: handles[i])

    def _get_uuid(h): return h["uuid"]
    def _get_name(h): return h["name"]

    def _get_mem(h):
        mem = MagicMock()
        mem.total = h["mem_total_bytes"]
        return mem

    def _get_power_limit(h):
        plm = h["power_limit_mw"]
        if plm is None:
            raise pynvml.NVMLError(pynvml.NVML_ERROR_NOT_SUPPORTED)
        return plm

    monkeypatch.setattr(pynvml, "nvmlDeviceGetUUID", _get_uuid)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetName", _get_name)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetMemoryInfo", _get_mem)
    monkeypatch.setattr(pynvml, "nvmlDeviceGetEnforcedPowerLimit", _get_power_limit)


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_discover_writes_inventory_json(tmp_path, monkeypatch):
    """Two GPUs → both files written in the expected shape, and
    discover() returns the matching list of GPUInventory."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"

    handles = [
        _make_handle(
            uuid="GPU-aaa",
            name="NVIDIA GeForce RTX 3090",
            mem_total_bytes=24576 * 1024 * 1024,  # 24576 MiB
            power_limit_mw=220_000,                # 220 W
        ),
        _make_handle(
            uuid="GPU-bbb",
            name="NVIDIA GeForce RTX 3090",
            mem_total_bytes=24576 * 1024 * 1024,
            power_limit_mw=220_000,
        ),
    ]
    _patch_nvml(monkeypatch, handles)

    result = inventory.discover(
        inventory_path=inv_path,
        config_path=cfg_path,
        version="2.0.0",
    )

    assert len(result) == 2
    assert [inv.index for inv in result] == [0, 1]
    assert [inv.uuid for inv in result] == ["GPU-aaa", "GPU-bbb"]

    inv_data = json.loads(inv_path.read_text())
    assert inv_data == {
        "gpus": [
            {"index": 0, "uuid": "GPU-aaa", "name": "NVIDIA GeForce RTX 3090",
             "memory_total_mib": 24576, "power_limit_w": 220},
            {"index": 1, "uuid": "GPU-bbb", "name": "NVIDIA GeForce RTX 3090",
             "memory_total_mib": 24576, "power_limit_w": 220},
        ]
    }

    cfg_data = json.loads(cfg_path.read_text())
    assert cfg_data["gpu_name"] == "NVIDIA GeForce RTX 3090"
    assert cfg_data["version"] == "2.0.0"
    assert cfg_data["gpus"] == inv_data["gpus"]


def test_discover_normalizes_bytes_uuid_and_name(tmp_path, monkeypatch):
    """Older nvidia-ml-py returns bytes from GetUUID/GetName; newer
    returns str. _to_str() handles both transparently."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"

    handles = [
        _make_handle(
            uuid=b"GPU-bytes-form",          # legacy bytes
            name=b"NVIDIA GeForce RTX 3090", # legacy bytes
            mem_total_bytes=8192 * 1024 * 1024,
            power_limit_mw=180_000,
        ),
    ]
    _patch_nvml(monkeypatch, handles)

    result = inventory.discover(
        inventory_path=inv_path,
        config_path=cfg_path,
        version="2.0.0",
    )

    assert result[0].uuid == "GPU-bytes-form"
    assert result[0].name == "NVIDIA GeForce RTX 3090"


def test_discover_handles_missing_power_limit(tmp_path, monkeypatch):
    """Some integrated/laptop GPUs don't expose power telemetry. The
    NVMLError on GetEnforcedPowerLimit is caught and the inventory
    records power_limit_w=0 — same fallback as bash for [N/A]."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"

    handles = [
        _make_handle(
            uuid="GPU-no-power",
            name="Integrated GPU",
            mem_total_bytes=2048 * 1024 * 1024,
            power_limit_mw=None,  # → triggers NVMLError
        ),
    ]
    _patch_nvml(monkeypatch, handles)

    result = inventory.discover(
        inventory_path=inv_path,
        config_path=cfg_path,
        version="2.0.0",
    )

    assert result[0].power_limit_w == 0


def test_discover_empty_falls_back_to_synthetic(tmp_path, monkeypatch):
    """No GPUs visible to NVML → synthetic single-GPU inventory
    (uuid='legacy-unknown'). Matches bash discover_gpus() behavior so
    the dashboard renders even on partial installs."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"

    monkeypatch.setattr(pynvml, "nvmlDeviceGetCount", lambda: 0)

    result = inventory.discover(
        inventory_path=inv_path,
        config_path=cfg_path,
        version="2.0.0",
    )

    assert len(result) == 1
    assert result[0].uuid == "legacy-unknown"
    assert result[0].index == 0
    assert result[0].name == "GPU"
    assert result[0].memory_total_mib == 0
    assert result[0].power_limit_w == 0

    inv_data = json.loads(inv_path.read_text())
    assert inv_data["gpus"][0]["uuid"] == "legacy-unknown"


def test_discover_atomic_write_uses_rename(tmp_path, monkeypatch):
    """Verifies _atomic_write_json: the .tmp file should not exist
    afterwards (rename moved it onto the target)."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"

    handles = [_make_handle(
        uuid="GPU-x", name="GPU", mem_total_bytes=1 << 30, power_limit_mw=100_000,
    )]
    _patch_nvml(monkeypatch, handles)

    inventory.discover(
        inventory_path=inv_path,
        config_path=cfg_path,
        version="2.0.0",
    )

    assert inv_path.exists()
    assert not (tmp_path / "gpu_inventory.json.tmp").exists()
    assert cfg_path.exists()
    assert not (tmp_path / "gpu_config.json.tmp").exists()


def test_discover_skips_failing_handle(tmp_path, monkeypatch):
    """If one GPU's metadata fetch raises NVMLError mid-loop, the
    remaining GPUs are still inventoried (no all-or-nothing)."""
    inv_path = tmp_path / "gpu_inventory.json"
    cfg_path = tmp_path / "gpu_config.json"

    monkeypatch.setattr(pynvml, "nvmlDeviceGetCount", lambda: 2)

    def _handle(i):
        if i == 0:
            raise pynvml.NVMLError(pynvml.NVML_ERROR_GPU_IS_LOST)
        return {
            "uuid": "GPU-ok",
            "name": "GPU 1",
            "mem_total_bytes": 1 << 30,
            "power_limit_mw": 100_000,
        }

    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex", _handle)
    # Accessor lambdas only used by index 1
    monkeypatch.setattr(pynvml, "nvmlDeviceGetUUID", lambda h: h["uuid"])
    monkeypatch.setattr(pynvml, "nvmlDeviceGetName", lambda h: h["name"])
    monkeypatch.setattr(pynvml, "nvmlDeviceGetMemoryInfo",
                        lambda h: MagicMock(total=h["mem_total_bytes"]))
    monkeypatch.setattr(pynvml, "nvmlDeviceGetEnforcedPowerLimit",
                        lambda h: h["power_limit_mw"])

    result = inventory.discover(
        inventory_path=inv_path,
        config_path=cfg_path,
        version="2.0.0",
    )

    assert len(result) == 1
    assert result[0].uuid == "GPU-ok"
