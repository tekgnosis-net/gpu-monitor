# Changelog

All notable changes to this fork of `bigsk1/gpu-monitor` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (Phase 2 — Multi-GPU collector)
- `discover_gpus()` function that queries `nvidia-smi --query-gpu=index,uuid,name,memory.total,power.max_limit` once at startup and populates per-GPU state (`NUM_GPUS`, `GPU_INDEXES`, `GPU_NAMES[idx]`, `GPU_UUIDS[idx]`).
- `/app/gpu_inventory.json` atomic inventory file written by `discover_gpus()`, read by `process_buffer.py` for per-row UUID lookup. Falls back to a synthetic single-GPU entry when `nvidia-smi` is unavailable (test / dev environments).
- `gpu_config.json` now carries a top-level `gpus: [...]` array alongside the legacy `gpu_name` key (`gpu_name` is preserved as `gpus[0].name` so the pre-Phase-3 frontend keeps rendering unchanged).
- Buffer file format extended from 5 fields (`timestamp,temp,util,mem,power`) to 6 fields (`timestamp,gpu_index,temp,util,mem,power`). The Phase 1 5-field format is still parsed as a backward-compat fallback so on-disk buffers from a pre-Phase-2 container flush cleanly after upgrade.
- `update_stats()` now emits one buffer line per attached GPU per tick. Each line is tagged with the GPU index; `process_buffer.py` looks up the matching UUID from `gpu_inventory.json` and records it in the DB row alongside `interval_s` (which is unchanged from Phase 1).
- `BUFFER_SIZE` is now derived as `ceil(flush_interval / interval) * NUM_GPUS`, keeping wall-clock flush cadence constant regardless of GPU count. Single-GPU default (4s/60s/1 GPU) still yields exactly 15, matching pre-overhaul behaviour.

### Changed
- Legacy flat files (`gpu_current_stats.json`, `history/history.json`, `gpu_24hr_stats.txt`) now filter on `gpu_index = 0` so the pre-Phase-3 frontend continues to see a single-GPU rendering even when the DB contains multi-GPU rows. Phase 3 will replace these with per-GPU API endpoints and delete the flat files.
- `process_buffer.py` heredoc looks up `gpu_uuid` per row via `gpu_inventory.json` (with `GPU_MONITOR_GPU_UUID` env var as last-resort fallback) instead of unconditionally using a single env var.

### Notes
- Multi-GPU support is rendered in the collector only; the frontend still shows only GPU 0 until Phase 3 ships the API-driven UI.
- Hot-add/remove of GPUs in a running container is out of Phase 2 scope — `discover_gpus()` runs once at startup. A container restart is required to pick up hardware changes.

### Added
- `VERSION` file as the single source of truth for the application version; exposed to the frontend via `gpu_config.json`.
- `CHANGELOG.md` following Keep-a-Changelog format; will be managed by release-please from v1.0.0 onwards.
- Database migration path with a new `migrate_database()` function in `monitor_gpu.sh`:
  - Adds a `gpu_index INTEGER NOT NULL DEFAULT 0` column to `gpu_metrics` (multi-GPU groundwork).
  - Adds a `gpu_uuid TEXT` column (audit/robustness).
  - Adds an `interval_s INTEGER NOT NULL DEFAULT 4` column so each row records the poll interval it was sampled at, keeping future power-integration math correct across settings changes.
  - Enables SQLite WAL journal mode for concurrent reader support.
  - Creates a composite `(gpu_index, timestamp_epoch)` index.
- Live-reload scaffolding: the collector reads `/app/settings.json` once per tick via `jq` and applies changes to `collection.interval_seconds` and `collection.flush_interval_seconds` without requiring a container restart. Falls back to defaults (`4s` / `60s`) if the file is missing.
- `BUFFER_SIZE` is now derived from `flush_interval_seconds / interval_seconds` instead of being hardcoded to 15.
- `GPU_UUID` is captured once at startup via `nvidia-smi --query-gpu=uuid` and written into every inserted metrics row so future multi-GPU and audit queries have a stable identifier.

### Notes
- This release is an internal foundation for the v1.0.0 overhaul. No user-visible behaviour changes.
- The WAL journal mode change creates `-wal` and `-shm` sidecar files alongside `history/gpu_metrics.db`. This is expected and safe.
- The migration is one-way — existing data is preserved but downgrading to a pre-migration image will fail because the old collector does not know about the new columns.

[Unreleased]: https://github.com/tekgnosis-net/gpu-monitor/compare/main...HEAD
