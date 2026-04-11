# Changelog

All notable changes to this fork of `bigsk1/gpu-monitor` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Phase 3** — aiohttp-based API routes on `src/server.py`:
  - `GET /api/health` — liveness + version + schema version
  - `GET /api/version` — `{version}`
  - `GET /api/gpus` — `{gpus: [...]}` from `/app/gpu_inventory.json`
  - `GET /api/metrics/current` — array of latest samples per GPU (correlated subquery on `(gpu_index, timestamp_epoch)` composite index)
  - `GET /api/metrics/history?range=24h&gpu=0` — per-GPU timeseries; range one of `15m|30m|1h|6h|12h|24h|3d|7d`, defaults to 24h on missing/invalid; shape matches the pre-Phase-3 `history/history.json` contract exactly so the retrofit is minimal
  - `GET /api/stats/24h` — array of `{gpu_index, stats: {min/max per metric}}`
- **Phase 3** — read-only SQLite connections per request (`file:...?mode=ro`) so the API boundary physically cannot issue writes. WAL journal mode from Phase 1 lets the collector's writes and API reads concur without locking.
- **Phase 3** — `tests/test_api.py` pytest integration suite (10 tests) covering every new endpoint, including range-param fallback and path-traversal rejection. `pytest.ini` and `tests/conftest.py` configure `asyncio_mode=auto` and exclude the legacy standalone load-test scripts from collection.
- **Phase 1** — `VERSION` file as the single source of truth for the application version; exposed to the frontend via `gpu_config.json`.
- **Phase 1** — `CHANGELOG.md` following Keep-a-Changelog format; will be managed by release-please from v1.0.0 onwards.
- **Phase 1** — Database migration path with a new `migrate_database()` function in `monitor_gpu.sh`:
  - Adds a `gpu_index INTEGER NOT NULL DEFAULT 0` column to `gpu_metrics` (multi-GPU groundwork).
  - Adds a `gpu_uuid TEXT` column (audit/robustness).
  - Adds an `interval_s INTEGER NOT NULL DEFAULT 4` column so each row records the poll interval it was sampled at, keeping future power-integration math correct across settings changes.
  - Enables SQLite WAL journal mode for concurrent reader support.
  - Creates a composite `(gpu_index, timestamp_epoch)` index.
- **Phase 1** — Live-reload scaffolding: the collector reads `/app/settings.json` once per tick via `jq` and applies changes to `collection.interval_seconds` and `collection.flush_interval_seconds` without requiring a container restart. Falls back to defaults (`4s` / `60s`) if the file is missing.
- **Phase 1** — `BUFFER_SIZE` is now derived from `flush_interval_seconds / interval_seconds` instead of being hardcoded to 15.
- **Phase 1** — `GPU_UUID` is captured once at startup via `nvidia-smi --query-gpu=uuid` and written into every inserted metrics row so future multi-GPU and audit queries have a stable identifier.
- **Phase 2** — `discover_gpus()` function that queries `nvidia-smi --query-gpu=index,uuid,name,memory.total,power.max_limit` once at startup and populates per-GPU state (`NUM_GPUS`, `GPU_INDEXES`, `GPU_NAMES[idx]`, `GPU_UUIDS[idx]`).
- **Phase 2** — `/app/gpu_inventory.json` atomic inventory file written by `discover_gpus()`, read by `process_buffer.py` for per-row UUID lookup. Falls back to a synthetic single-GPU entry when `nvidia-smi` is unavailable (test / dev environments).
- **Phase 2** — `gpu_config.json` now carries a top-level `gpus: [...]` array alongside the legacy `gpu_name` key (`gpu_name` is preserved as `gpus[0].name` so the pre-Phase-3 frontend keeps rendering unchanged).
- **Phase 2** — Buffer file format extended from 5 fields (`timestamp,temp,util,mem,power`) to 6 fields (`timestamp,gpu_index,temp,util,mem,power`). The Phase 1 5-field format is still parsed as a backward-compat fallback so on-disk buffers from a pre-Phase-2 container flush cleanly after upgrade.
- **Phase 2** — `update_stats()` now emits one buffer line per attached GPU per tick. Each line is tagged with the GPU index; `process_buffer.py` looks up the matching UUID from `gpu_inventory.json` and records it in the DB row alongside `interval_s`.
- **Phase 2** — `BUFFER_SIZE` is now `ceil(flush_interval / interval) * NUM_GPUS`, keeping wall-clock flush cadence constant regardless of GPU count. The computation is triggered by any change in interval, flush, or `NUM_GPUS` so a multi-GPU install on default settings scales correctly at startup.
- **Phase 2** — Hot-remove detection: `update_stats()` logs a WARNING if `nvidia-smi` returns fewer rows than `discover_gpus` found at startup, so a silently-removed GPU becomes observable.

### Changed
- **Phase 3** — `src/server.py` grew from 30 lines (static-only) to ~300 lines with the aiohttp JSON API. Static file serving still works via a catch-all registered last. Module now exposes `make_app()` so tests can construct fresh app instances per test.
- **Phase 3** — `src/web/gpu-stats.html` fetch call sites retrofitted to hit `/api/metrics/current`, `/api/metrics/history`, `/api/stats/24h` instead of the deleted flat files. Response shapes are compatible with the existing downstream render code via minimal adapter layers. `gpu_config.json` stays as a static file. This is a transitional retrofit; Phase 4 rewrites the whole frontend.
- **Phase 2** — Legacy flat files (`gpu_current_stats.json`, `history/history.json`, `gpu_24hr_stats.txt`) now filter on `gpu_index = 0` so the pre-Phase-3 frontend continues to see a single-GPU rendering even when the DB contains multi-GPU rows. Phase 3 will replace these with per-GPU API endpoints and delete the flat files.

### Removed
- **Phase 3** — `process_historical_data` and `process_24hr_stats` functions (and their embedded Python heredocs `export_json.py` / `process_stats.py`) deleted from `src/monitor_gpu.sh`. The collector no longer writes `history/history.json` or `gpu_24hr_stats.txt` — those files will simply stop appearing in the volume after the upgrade. `STATS_FILE` bash constant removed. Stale references to `export_history_json` in comments updated. `gpu_current_stats.json` is still written as a transitional shim for the pre-Phase-4 frontend and will be removed in Phase 4.
- **Phase 2** — `process_buffer.py` heredoc looks up `gpu_uuid` per row via `gpu_inventory.json` (with `GPU_MONITOR_GPU_UUID` env var as last-resort fallback) instead of unconditionally using a single env var.
- **Phase 2** — CSV whitespace trimming in `update_stats()` uses bash parameter expansion instead of `echo | xargs`, removing a subprocess fork per field per GPU per tick.

### Notes
- This release is an internal foundation for the v1.0.0 overhaul. No user-visible behaviour changes yet; multi-GPU support lives in the collector and DB only, and the frontend still shows only GPU 0 until Phase 3 ships the API-driven UI.
- The WAL journal mode change creates `-wal` and `-shm` sidecar files alongside `history/gpu_metrics.db`. This is expected and safe.
- The Phase 1 migration is one-way — existing data is preserved but downgrading to a pre-Phase-1 image will fail because the old collector does not know about the new columns.
- Hot-add/remove of GPUs in a running container is out of Phase 2 scope — `discover_gpus()` runs once at startup; a container restart is required to pick up hardware changes.

[Unreleased]: https://github.com/tekgnosis-net/gpu-monitor/compare/main...HEAD
