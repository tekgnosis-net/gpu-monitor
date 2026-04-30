"""GPU inventory discovery via NVML.

Replaces the bash `discover_gpus()` function. Writes
`/app/gpu_inventory.json` (consumed by server.py /api/gpus and the
collector at runtime) and `/app/gpu_config.json` (consumed by the
frontend).

Filled in during Step 3 of the refactor.
"""
