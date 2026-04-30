"""Tests for `gpu_monitor.source.NVMLSource`.

The module's contract is small but high-stakes: per-tick NVML
queries that must tolerate per-GPU error states (NOT_SUPPORTED for
power, GPU_IS_LOST mid-loop) without dropping the whole sample.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pynvml

from gpu_monitor.source import NVMLSource
from gpu_monitor.state import GPUInventory, GPUMetric


def _inv(index, uuid):
    return GPUInventory(
        index=index, uuid=uuid, name=f"GPU {index}",
        memory_total_mib=24576, power_limit_w=220,
    )


def _patch_nvml(monkeypatch, *, per_handle):
    """`per_handle` is a dict mapping handle_id → {method_name: spec},
    where spec is either a return value or an Exception instance.

    SimpleNamespace handles (rather than MagicMock) avoid the
    is-callable trap: MagicMock instances are callable, so a "return
    this object" spec would have been miscalled as a factory.

    The GetMemoryInfo dispatch handles both v1 (`GetMemoryInfo(h)`)
    and v2 (`GetMemoryInfo(h, version=...)`) calls — by default the
    same spec serves both. Tests that need to differentiate v1 vs v2
    can use the "GetMemoryInfo_v2" key for the version-tagged path."""
    handles = {i: SimpleNamespace(_id=i) for i in per_handle}
    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex",
                        lambda i: handles[i])

    def _dispatch(method_name):
        def _impl(handle, *args, **kwargs):
            for hid, h in handles.items():
                if h is handle:
                    spec_key = method_name
                    # Distinguish v2 memory calls from v1 if the test
                    # opts in by providing a "GetMemoryInfo_v2" key.
                    if (method_name == "GetMemoryInfo"
                            and kwargs.get("version") is not None
                            and "GetMemoryInfo_v2" in per_handle[hid]):
                        spec_key = "GetMemoryInfo_v2"
                    spec = per_handle[hid].get(spec_key)
                    if isinstance(spec, BaseException):
                        raise spec
                    return spec
            raise AssertionError(f"unknown handle: {handle!r}")
        return _impl

    monkeypatch.setattr(pynvml, "nvmlDeviceGetTemperature",
                        _dispatch("GetTemperature"))
    monkeypatch.setattr(pynvml, "nvmlDeviceGetUtilizationRates",
                        _dispatch("GetUtilizationRates"))
    monkeypatch.setattr(pynvml, "nvmlDeviceGetMemoryInfo",
                        _dispatch("GetMemoryInfo"))
    monkeypatch.setattr(pynvml, "nvmlDeviceGetPowerUsage",
                        _dispatch("GetPowerUsage"))


def test_sample_happy_path(monkeypatch):
    """Both GPUs healthy → both metrics returned with the right
    types and unit conversions."""
    util_obj = SimpleNamespace(gpu=73, memory=42)
    mem_obj = SimpleNamespace(used=24060 * 1024 * 1024)  # 24060 MiB

    _patch_nvml(monkeypatch, per_handle={
        0: {
            "GetTemperature": 63,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": mem_obj,
            "GetPowerUsage": 215_870,  # 215.87 W in mW
        },
        1: {
            "GetTemperature": 66,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": mem_obj,
            "GetPowerUsage": 215_420,
        },
    })

    src = NVMLSource([_inv(0, "GPU-aaa"), _inv(1, "GPU-bbb")])
    out = src.sample()
    assert len(out) == 2
    assert all(isinstance(m, GPUMetric) for m in out)

    m0 = out[0]
    assert m0.gpu_index == 0
    assert m0.gpu_uuid == "GPU-aaa"
    assert m0.temperature == 63.0
    assert m0.utilization == 73.0
    assert m0.memory_mib == 24060.0
    assert m0.power_w == 215.87  # mW → W
    assert isinstance(m0.timestamp_epoch, int)
    # Should be roughly "now"
    assert abs(m0.timestamp_epoch - int(time.time())) < 5


def test_sample_power_not_supported_returns_none(monkeypatch):
    """NVML_ERROR_NOT_SUPPORTED on power → power_w=None (→ SQL NULL).
    This is the v1.5.0 telemetry-gap contract, now driven by NVML's
    structured error code rather than parsing a `[N/A]` string."""
    util_obj = SimpleNamespace(gpu=10, memory=5)
    mem_obj = SimpleNamespace(used=1024 * 1024 * 1024)

    err_not_supported = pynvml.NVMLError(pynvml.NVML_ERROR_NOT_SUPPORTED)

    _patch_nvml(monkeypatch, per_handle={
        0: {
            "GetTemperature": 50,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": mem_obj,
            "GetPowerUsage": err_not_supported,
        },
    })

    src = NVMLSource([_inv(0, "GPU-no-power")])
    out = src.sample()
    assert len(out) == 1
    assert out[0].power_w is None
    # Other fields still populated
    assert out[0].temperature == 50.0


def test_sample_gpu_lost_skips_that_gpu(monkeypatch):
    """If a GPU vanishes mid-loop (NVML_ERROR_GPU_IS_LOST on
    temperature read), that GPU is skipped — not the whole sample."""
    util_obj = SimpleNamespace(gpu=20, memory=10)
    mem_obj = SimpleNamespace(used=2 * 1024 * 1024 * 1024)
    err_lost = pynvml.NVMLError(pynvml.NVML_ERROR_GPU_IS_LOST)

    _patch_nvml(monkeypatch, per_handle={
        0: {
            "GetTemperature": err_lost,  # GPU 0 vanishes
        },
        1: {
            "GetTemperature": 55,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": mem_obj,
            "GetPowerUsage": 100_000,
        },
    })

    src = NVMLSource([_inv(0, "lost"), _inv(1, "ok")])
    out = src.sample()
    # Only the surviving GPU reports
    assert len(out) == 1
    assert out[0].gpu_index == 1
    assert out[0].gpu_uuid == "ok"


def test_sample_unexpected_power_error_logs_null(monkeypatch):
    """Power read raises a non-NOT_SUPPORTED NVMLError → power_w=None,
    other fields preserved. We don't want a transient driver hiccup
    to drop the whole sample."""
    util_obj = SimpleNamespace(gpu=10, memory=5)
    mem_obj = SimpleNamespace(used=1024 * 1024 * 1024)
    err_unknown = pynvml.NVMLError(pynvml.NVML_ERROR_UNKNOWN)

    _patch_nvml(monkeypatch, per_handle={
        0: {
            "GetTemperature": 50,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": mem_obj,
            "GetPowerUsage": err_unknown,
        },
    })

    src = NVMLSource([_inv(0, "x")])
    out = src.sample()
    assert len(out) == 1
    assert out[0].power_w is None


def test_sample_prefers_v2_memory_api(monkeypatch):
    """When NVML v2 memory API is available, NVMLSource calls it for
    `used` so the value matches what nvidia-smi reports. The v1 API
    overcounts by ~400 MiB on Ampere/Ada cards (driver-reserved
    chunk). This test gives different specs for v1 and v2 and asserts
    the v2 value lands in the GPUMetric."""
    util_obj = SimpleNamespace(gpu=10, memory=5)
    v1_mem = SimpleNamespace(used=24570 * 1024 * 1024)  # legacy total-free
    v2_mem = SimpleNamespace(used=24120 * 1024 * 1024)  # nvidia-smi-matching

    _patch_nvml(monkeypatch, per_handle={
        0: {
            "GetTemperature": 50,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": v1_mem,
            "GetMemoryInfo_v2": v2_mem,
            "GetPowerUsage": 200_000,
        },
    })

    src = NVMLSource([_inv(0, "x")])
    out = src.sample()
    assert len(out) == 1
    assert out[0].memory_mib == 24120.0, (
        f"v2 memory API should win when available; got {out[0].memory_mib}"
    )


def test_sample_falls_back_to_v1_memory_when_v2_unavailable(monkeypatch):
    """Older drivers / pynvml releases without nvmlMemory_v2 fall
    back to v1. The result is overcounted but still useful — better
    than a hard failure."""
    util_obj = SimpleNamespace(gpu=10, memory=5)
    v1_mem = SimpleNamespace(used=24570 * 1024 * 1024)

    _patch_nvml(monkeypatch, per_handle={
        0: {
            "GetTemperature": 50,
            "GetUtilizationRates": util_obj,
            "GetMemoryInfo": v1_mem,
            "GetPowerUsage": 200_000,
        },
    })
    # Hide the v2 sentinel so NVMLSource constructor flips the flag off
    monkeypatch.delattr(pynvml, "nvmlMemory_v2", raising=False)

    src = NVMLSource([_inv(0, "x")])
    assert src._mem_v2_available is False
    out = src.sample()
    assert out[0].memory_mib == 24570.0


def test_init_skips_handle_acquisition_failure(monkeypatch):
    """If pynvml.nvmlDeviceGetHandleByIndex raises at construction,
    that GPU is dropped from the cache (no exception bubbled)."""
    err = pynvml.NVMLError(pynvml.NVML_ERROR_GPU_IS_LOST)

    def _handle(i):
        if i == 0:
            raise err
        return SimpleNamespace(_id=i)

    monkeypatch.setattr(pynvml, "nvmlDeviceGetHandleByIndex", _handle)

    src = NVMLSource([_inv(0, "lost"), _inv(1, "ok")])
    # Even though sample() is now stubbed-out for GPU 1, we don't need
    # to drive it — just verify the cache excludes GPU 0.
    assert len(src._handles) == 1
    assert src._handles[0][0].uuid == "ok"
