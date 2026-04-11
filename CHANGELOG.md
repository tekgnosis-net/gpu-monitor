# Changelog

All notable changes to this fork of `bigsk1/gpu-monitor` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
