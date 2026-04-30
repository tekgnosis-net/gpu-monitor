"""NVML-based metric source.

Replaces the bash `nvidia-smi --query-gpu=...` subprocess invocation
with direct calls into `libnvidia-ml.so` via `pynvml`. Returns
structured `GPUMetric` instances; missing power telemetry surfaces as
`power_w=None` rather than the legacy `[N/A]` string sentinel.

Filled in during Step 4 of the refactor.
"""
