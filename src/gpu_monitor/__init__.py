"""gpu_monitor — async Python collector for NVIDIA GPU metrics.

Replaces the legacy bash collector (`monitor_gpu.sh`) with a single asyncio
process that supervises the metric collector, the aiohttp web server, the
report scheduler, and the alert checker. Calls NVML directly via
`nvidia-ml-py` (importable as `pynvml`) — no subprocess fork per tick, no
CSV staging, no `.pending`/`.stuck-*` retry ladder.
"""

__version__ = "2.0.0"
