#!/bin/bash
###############################################################################
# GPU Monitor - Backend Process
# 
# This script monitors NVIDIA GPU metrics and provides real-time data for the
# dashboard. It handles:
# - Real-time GPU metrics collection
# - Historical data management
# - Log rotation and cleanup
# - Data persistence through system updates
# - Error recovery and resilience
#
# Dependencies:
# - nvidia-smi
# - Python 3.12+
# - SQLite3
# - Basic Unix utilities
###############################################################################

BASE_DIR="/app"
LOG_FILE="$BASE_DIR/gpu_stats.log"
# NOTE (Phase 4): JSON_FILE (gpu_current_stats.json) has been deleted.
# The pre-Phase-4 frontend read it as a legacy single-GPU current-stats
# shim; Phase 4's UI rewrite reads /api/metrics/current directly from
# the DB via server.py. Phase 3 already deleted history.json and
# gpu_24hr_stats.txt; Phase 4 now removes the last flat-file write
# from the collector, leaving only the SQLite DB + inventory + config.
HISTORY_DIR="$BASE_DIR/history"
LOG_DIR="$BASE_DIR/logs"
ERROR_LOG="$LOG_DIR/error.log"
WARNING_LOG="$LOG_DIR/warning.log"
DEBUG_LOG="$LOG_DIR/debug.log"
BUFFER_FILE="/tmp/stats_buffer"
# SQLite database location
DB_FILE="$HISTORY_DIR/gpu_metrics.db"
# Settings file (read each tick for live reload, see load_settings())
SETTINGS_FILE="$BASE_DIR/settings.json"
# Version file (written into gpu_config.json at startup for the frontend)
VERSION_FILE="$BASE_DIR/VERSION"

# Collection cadence — these are the DEFAULTS. The real values are overridden
# by settings.json at runtime via load_settings() if that file exists, which
# lets the user tune cadence live without a container restart.
INTERVAL=4           # Time between GPU checks (seconds)
FLUSH_INTERVAL=60    # Buffered readings are committed to the DB this often (seconds)
BUFFER_SIZE=15       # Derived: ceil(FLUSH_INTERVAL / INTERVAL). Recomputed in load_settings().

# Single source of truth for the history retention window.
# Used by clean_old_data (DB purge). Phase 3 deleted the flat-file
# export_history_json heredoc that also referenced this constant, so
# clean_old_data is the sole remaining consumer until Phase 6 sources
# the value from housekeeping.retention_days in settings.json.
# Default: 3 days + 10 minutes of slack to avoid chart gaps at the edge.
RETENTION_SECONDS=$(( 3 * 86400 + 600 ))

# Debug toggle (comment out to disable debug logging)
# DEBUG=true

# Create required directories
mkdir -p "$LOG_DIR"
mkdir -p "$HISTORY_DIR"

# Read the application version once at startup. Used by the frontend (via
# gpu_config.json) and by the Phase 3 /api/version route. A missing VERSION
# file falls back to "unknown" rather than crashing so local/dev runs are
# forgiving.
if [ -r "$VERSION_FILE" ]; then
    GPU_MONITOR_VERSION=$(tr -d '[:space:]' < "$VERSION_FILE")
else
    GPU_MONITOR_VERSION="unknown"
fi
export GPU_MONITOR_VERSION

###############################################################################
# Logging Functions
# These functions handle different levels of logging with timestamps
###############################################################################

# Log error messages to both console and error log file
log_error() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] ERROR: $1" | tee -a "$ERROR_LOG"
}

# Log warning messages to warning log file
log_warning() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] WARNING: $1" | tee -a "$WARNING_LOG"
}

# Log debug messages when debug mode is enabled
log_debug() {
    if [ "${DEBUG:-}" = "true" ]; then
        local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
        echo "[$timestamp] DEBUG: $1" >> "$DEBUG_LOG"
    fi
}

###############################################################################
# _trim_ws: Strip leading/trailing whitespace from a string using only bash
# parameter expansion — no subprocess fork, no pipeline. Writes the result
# to the global $REPLY variable in the caller's scope (idiomatic bash
# pattern for return-by-reference helpers in hot paths).
#
# Used in update_stats()'s per-tick CSV parsing loop. Phase 2's initial
# version called `echo ... | xargs` per field, which forked one subprocess
# per CSV field per GPU per tick — ~16 forks per tick on a 4-GPU system.
# Parameter expansion is strictly cheaper (no fork, no exec).
#
# Defined at the top level (not inside update_stats) because bash functions
# are globally scoped regardless of where they're declared — defining it
# inside update_stats would re-register the function on every tick.
###############################################################################
_trim_ws() {
    local v="${1#"${1%%[![:space:]]*}"}"
    REPLY="${v%"${v##*[![:space:]]}"}"
}

# Paths for GPU inventory and config. Discovery happens later in the
# startup block once helper functions (safe_write_json, log_*) are in
# scope — see the GPU inventory section near the end of this file.
INVENTORY_FILE="$BASE_DIR/gpu_inventory.json"
CONFIG_FILE="$BASE_DIR/gpu_config.json"

# Multi-GPU state populated by discover_gpus():
#   NUM_GPUS        — integer count of attached GPUs (>=1)
#   GPU_INDEXES     — array of integer indexes as reported by nvidia-smi
#   GPU_NAMES[idx]  — associative: display name per index
#   GPU_UUIDS[idx]  — associative: stable audit UUID per index
# The indexes are strings in the associative arrays (bash array semantics)
# but always parseable as integers since nvidia-smi emits them as such.
#
# Legacy GPU_NAME / GPU_UUID / GPU_INDEX scalars are kept in sync with
# gpus[0] so the Phase 1 code paths (single-GPU flat files, process_buffer
# env fallback) continue to work unchanged.
declare -gA GPU_NAMES
declare -gA GPU_UUIDS
declare -ga GPU_INDEXES

# NVIDIA_SMI_AVAILABLE — cached at startup via `command -v nvidia-smi`.
#
# Dev/test environments frequently have no nvidia-smi binary at all
# (CI runners, container unit tests, non-NVIDIA Linux boxes). Without
# this flag, every tick of update_stats calls nvidia-smi, gets empty
# output, returns 1, and engages the main loop's 3×1s retry backoff —
# producing ~4 log lines per tick and ~10 seconds of wall-clock spent
# retrying a failure that will never recover. The loop never quits
# either, so dev environments "work" but the log files fill with
# false-alarm ERRORs.
#
# The fix: at startup, run `command -v nvidia-smi` once and cache
# the result. update_stats consults the cache — if nvidia-smi is
# permanently unavailable, synthesize zeroed metric rows for every
# discovered GPU and return 0, bypassing the retry loop entirely.
# If nvidia-smi IS installed but returns empty (transient failure,
# driver hiccup, permissions), the old retry path still engages
# because that's the case the retry was designed for.
#
# Set to 0 before the detection runs; the startup block a few
# hundred lines below flips it to 1 if the binary is found.
NVIDIA_SMI_AVAILABLE=0
NUM_GPUS=0
GPU_NAME="GPU"
GPU_UUID="legacy-unknown"
GPU_INDEX=0
export GPU_UUID

# NOTE: gpu_config.json is WRITTEN in the startup block near the end of
# this file, after all helper functions (safe_write_json in particular)
# have been defined. Top-level script flow in bash runs line-by-line, so
# calling safe_write_json up here would fail because its definition is
# further down.

###############################################################################
# load_settings: Recomputes the effective collection cadence every tick and
# applies any change to INTERVAL / FLUSH_INTERVAL / BUFFER_SIZE. Called once
# per collector tick so the user can change cadence live from the Settings UI
# without restarting the container (propagation delay: at most one tick).
#
# Behaviour matrix:
#   - settings.json missing / unreadable / malformed        → defaults
#   - settings.json present but collection.* keys missing   → defaults
#   - settings.json present but values out of range         → defaults
#   - settings.json present with valid values               → those values
#
# Crucially the defaults case is re-asserted every tick, so deleting
# settings.json while the container is running reverts the cadence to
# (4s / 60s / 15), matching the pre-overhaul script behaviour.
#
# Note on jq: at the function level this file appears to tolerate jq
# being absent (we fall back to defaults), but jq is NOT optional for
# the collector as a whole. The startup block later runs `jq -n` to
# generate gpu_config.json and exits 1 if jq is missing. Keeping the
# local jq-availability check here is belt-and-suspenders for test
# harnesses that exercise load_settings in isolation.
#
# Validation: interval_seconds must be in [2, 300]; flush_interval_seconds
# must be in [5, 3600]; anything outside falls back to the default. BUFFER_SIZE
# is derived as ceil(FLUSH_INTERVAL / INTERVAL) with a minimum of 1.
#
# Only logs a message when a value actually changes — keeps the logs quiet
# in the steady state.
###############################################################################
function load_settings() {
    local default_interval=4
    local default_flush=60
    local new_interval="$default_interval"
    local new_flush="$default_flush"

    # Compute the candidate values from settings.json if it's readable AND
    # jq is installed; otherwise stick with the defaults already assigned.
    if [ -r "$SETTINGS_FILE" ] && command -v jq >/dev/null 2>&1; then
        new_interval=$(jq -r '.collection.interval_seconds // empty' "$SETTINGS_FILE" 2>/dev/null)
        new_flush=$(jq -r '.collection.flush_interval_seconds // empty' "$SETTINGS_FILE" 2>/dev/null)
    fi

    # Validate interval_seconds ∈ [2, 300]; fall back to default otherwise.
    if ! [[ "$new_interval" =~ ^[0-9]+$ ]] || [ "$new_interval" -lt 2 ] || [ "$new_interval" -gt 300 ]; then
        new_interval="$default_interval"
    fi
    # Validate flush_interval_seconds ∈ [5, 3600]; fall back to default otherwise.
    if ! [[ "$new_flush" =~ ^[0-9]+$ ]] || [ "$new_flush" -lt 5 ] || [ "$new_flush" -gt 3600 ]; then
        new_flush="$default_flush"
    fi

    # Cross-field coherence: the flush cadence must be at least as long as
    # the collection cadence, otherwise BUFFER_SIZE collapses to 1 and we
    # commit every tick. Clamping up (rather than rejecting) honours the
    # user's intent of "as tight a flush as possible" while keeping the
    # logged/advertised value matched to actual behaviour.
    if [ "$new_flush" -lt "$new_interval" ]; then
        new_flush="$new_interval"
    fi

    # Derive the intended BUFFER_SIZE from the CURRENT values of
    # new_interval / new_flush / NUM_GPUS. Doing this unconditionally means
    # the diff check below catches three orthogonal sources of change:
    #   1. User edited settings.json (interval and/or flush)
    #   2. discover_gpus() ran and populated NUM_GPUS > 1 at startup (the
    #      first load_settings() call after startup will see the new count)
    #   3. Some future path that mutates NUM_GPUS live (Phase 2 doesn't
    #      have one, but the diff check is structured so it would Just Work)
    # Without this, a multi-GPU install on default settings would never
    # scale BUFFER_SIZE past the hardcoded 15, shortening the effective
    # wall-clock flush cadence to 1/N of intended.
    local ticks_per_flush=$(( (new_flush + new_interval - 1) / new_interval ))
    [ "$ticks_per_flush" -lt 1 ] && ticks_per_flush=1
    local new_buffer_size=$(( ticks_per_flush * ${NUM_GPUS:-1} ))
    [ "$new_buffer_size" -lt 1 ] && new_buffer_size=1

    if [ "$new_interval" != "$INTERVAL" ] \
       || [ "$new_flush" != "$FLUSH_INTERVAL" ] \
       || [ "$new_buffer_size" != "$BUFFER_SIZE" ]; then
        # CRITICAL: flush any buffered rows BEFORE applying the new interval
        # so they get written with the cadence they were actually sampled at.
        # Without this, rows buffered under the old INTERVAL would be flushed
        # with the new $INTERVAL as their interval_s, silently corrupting
        # Phase 5's power-integration math around every cadence change.
        # process_buffer reads GPU_MONITOR_INTERVAL_S from the environment,
        # which is still the OLD $INTERVAL at this point.
        if [ -f "$BUFFER_FILE" ] && [ -s "$BUFFER_FILE" ]; then
            log_debug "Flushing ${INTERVAL}s-interval buffer before applying new cadence"
            if ! process_buffer; then
                # Best-effort semantics: log the failure for visibility and
                # leave $INTERVAL / $FLUSH_INTERVAL / $BUFFER_SIZE untouched.
                # Swapping cadence on a failed flush would attribute future
                # rows to the new interval even though the user's request
                # was never fully honoured for the current data. The diff
                # check above keeps firing until the values match, so the
                # NEXT tick automatically retries the flush — no propagation
                # into update_stats' data-collection retry loop (which is
                # tuned for nvidia-smi hiccups, not settings-reload issues).
                log_warning "Failed to flush ${INTERVAL}s-interval buffer; postponing cadence change (will retry next tick)"
                return 0
            fi
            # Phase 3 deletes process_historical_data and process_24hr_stats
            # (see the removed-functions note block further down). The
            # collector just writes the DB; Phase 3's /api/metrics/history
            # and /api/stats/24h routes query SQLite directly, so there is
            # nothing to export here after the flush succeeds.
        fi

        INTERVAL="$new_interval"
        FLUSH_INTERVAL="$new_flush"
        BUFFER_SIZE="$new_buffer_size"
        log_warning "Collection settings reloaded: interval=${INTERVAL}s flush=${FLUSH_INTERVAL}s buffer_size=${BUFFER_SIZE} (ticks_per_flush=${ticks_per_flush} × num_gpus=${NUM_GPUS:-1})"
    fi
    return 0
}

###############################################################################
# discover_gpus: Queries nvidia-smi for all attached GPUs and populates the
# global NUM_GPUS / GPU_INDEXES / GPU_NAMES / GPU_UUIDS state used by
# update_stats, process_buffer, and the gpu_config.json / gpu_inventory.json
# writers. Called once at startup; not re-run on every tick (hot-add/remove
# of GPUs in a running container is outside Phase 2 scope).
#
# Also writes /app/gpu_inventory.json, which process_buffer.py reads to
# look up gpu_uuid per gpu_index. safe_write_json provides atomic replace.
#
# Falls back cleanly when nvidia-smi is unavailable (test harness, dev env):
#   - Produces one synthetic GPU with index=0, name="GPU",
#     uuid="legacy-unknown", memory_total_mib=0, power_limit_w=0
#   - Everything downstream continues to work in single-GPU mode
###############################################################################
function discover_gpus() {
    # Clear any prior state (useful if this is ever called more than once).
    GPU_INDEXES=()
    GPU_NAMES=()
    GPU_UUIDS=()
    NUM_GPUS=0

    # Structured query: one row per GPU, comma-separated, no header.
    # Fields: index, uuid, name, memory.total (MiB), power.max_limit (W).
    local csv
    csv=$(nvidia-smi \
        --query-gpu=index,uuid,name,memory.total,power.max_limit \
        --format=csv,noheader,nounits 2>/dev/null)

    # Collect per-GPU entries for the inventory JSON.
    local inventory_entries=""

    if [ -n "$csv" ]; then
        while IFS=',' read -r idx uuid name mem_total power_limit; do
            # Strip leading/trailing whitespace that nvidia-smi adds after
            # every comma separator. Uses the in-process _trim_ws helper
            # rather than `echo ... | xargs` (which forks two processes
            # per field = 10 subprocess forks per GPU at startup and
            # adds a runtime dependency on xargs) for consistency with
            # the same trim pattern in update_stats() — see the helper
            # definition near the top of this file for rationale.
            _trim_ws "$idx";          idx="$REPLY"
            _trim_ws "$uuid";         uuid="$REPLY"
            _trim_ws "$name";         name="$REPLY"
            _trim_ws "$mem_total";    mem_total="$REPLY"
            _trim_ws "$power_limit";  power_limit="$REPLY"

            # Validate idx as a non-negative integer before touching any
            # accumulator state. A non-numeric row (error message leaked
            # past 2>/dev/null, unexpected nvidia-smi output format) that
            # only failed the empty-string check would corrupt the
            # NUM_GPUS counter and build an invalid jq --argjson entry,
            # preventing the synthetic-fallback path below from rescuing
            # the startup.
            if [[ ! "$idx" =~ ^[0-9]+$ ]]; then
                log_warning "discover_gpus: skipping non-numeric index from nvidia-smi: ${idx:-(empty)}"
                continue
            fi
            [ -z "$uuid" ] && uuid="legacy-unknown"
            [ -z "$name" ] && name="GPU"
            # mem_total and power_limit may be [N/A] or [Not Supported]
            # on some hardware. Use proper JSON-number regexes (not
            # loose [0-9.]) so edge cases like "." or "1.2.3" don't slip
            # through and break `jq --argjson` downstream.
            [[ ! "$mem_total" =~ ^[0-9]+$ ]] && mem_total=0
            [[ ! "$power_limit" =~ ^[0-9]+(\.[0-9]+)?$ ]] && power_limit=0

            GPU_INDEXES+=("$idx")
            GPU_NAMES[$idx]="$name"
            GPU_UUIDS[$idx]="$uuid"
            NUM_GPUS=$(( NUM_GPUS + 1 ))

            # Build a JSON entry per GPU. jq would be cleaner but we'd have
            # to build and merge per-GPU fragments; a plain string is fine
            # here because every field is already sanitised (indexes/memory
            # are numeric, uuid/name are trusted vendor output).
            local entry
            entry=$(jq -n \
                --argjson index "$idx" \
                --arg uuid "$uuid" \
                --arg name "$name" \
                --argjson memory_total_mib "$mem_total" \
                --argjson power_limit_w "$power_limit" \
                '{index: $index, uuid: $uuid, name: $name, memory_total_mib: $memory_total_mib, power_limit_w: $power_limit_w}')
            if [ -z "$inventory_entries" ]; then
                inventory_entries="$entry"
            else
                inventory_entries="$inventory_entries"$'\n'"$entry"
            fi
        done <<< "$csv"
    fi

    # Synthetic single-GPU fallback when nvidia-smi returned nothing.
    if [ "$NUM_GPUS" -eq 0 ]; then
        log_warning "discover_gpus: nvidia-smi returned no GPUs; falling back to synthetic single-GPU inventory"
        GPU_INDEXES=("0")
        GPU_NAMES[0]="GPU"
        GPU_UUIDS[0]="legacy-unknown"
        NUM_GPUS=1
        inventory_entries=$(jq -n \
            '{index: 0, uuid: "legacy-unknown", name: "GPU", memory_total_mib: 0, power_limit_w: 0}')
    fi

    # Update the legacy scalar state so Phase 1 code paths that still refer
    # to GPU_NAME / GPU_UUID / GPU_INDEX keep working (flat-file writers,
    # process_buffer env fallback).
    GPU_INDEX="${GPU_INDEXES[0]}"
    GPU_NAME="${GPU_NAMES[$GPU_INDEX]}"
    GPU_UUID="${GPU_UUIDS[$GPU_INDEX]}"
    export GPU_UUID

    # Wrap the per-GPU entries in a top-level object and write atomically.
    # Two separate failure modes to catch:
    #  (a) jq can't parse/merge the per-GPU entries — indicates a bug
    #      in the entry-building code above, fail fast rather than
    #      letting safe_write_json persist an empty/invalid inventory.
    #  (b) safe_write_json can't write the file — disk full, permissions,
    #      read-only mount. Fail fast rather than running with a stale
    #      inventory file and silently falling back to the single-UUID
    #      env var for multi-GPU installs.
    local inventory_json
    if ! inventory_json=$(jq -s '{gpus: .}' <<< "$inventory_entries"); then
        log_error "discover_gpus: failed to build GPU inventory JSON (jq error)"
        return 1
    fi
    if ! safe_write_json "$INVENTORY_FILE" "$inventory_json"; then
        log_error "discover_gpus: failed to write GPU inventory to $INVENTORY_FILE"
        return 1
    fi

    log_debug "discover_gpus: detected $NUM_GPUS GPU(s): ${GPU_INDEXES[*]}"
    return 0
}

###############################################################################
# gpu_metrics_has_column: Asks SQLite whether the given column exists on the
# gpu_metrics table. Uses the queryable pragma_table_info() form rather than
# piping CLI output through awk, which would be fragile if the sqlite3 CLI
# output format is changed by a user sqliterc (-header/-csv/etc.). Returns 0
# when the column exists, 1 otherwise.
#
# The column name is always a hardcoded identifier from this file (never
# external input), so direct string interpolation into the SQL is safe.
###############################################################################
function gpu_metrics_has_column() {
    local col="$1"
    local result
    result=$(sqlite3 -init /dev/null "$DB_FILE" \
        "SELECT 1 FROM pragma_table_info('gpu_metrics') WHERE name='${col}';" 2>/dev/null)
    [ "$result" = "1" ]
}

###############################################################################
# migrate_database: Idempotently upgrades the gpu_metrics table to the Phase 1
# schema. Adds three columns on existing installs:
#   - gpu_index  (multi-GPU groundwork; Phase 2 starts writing values > 0)
#   - gpu_uuid   (audit trail / robustness against nvidia-smi index shuffle)
#   - interval_s (per-row sample interval, so future power integration stays
#                 correct across settings changes — see Phase 5)
# Also enables WAL journal mode for concurrent reader support.
#
# Safe to call every boot. Returns 0 on success (including no-op), 1 on
# any ALTER/PRAGMA failure. Callers must check the return status.
###############################################################################
function migrate_database() {
    log_debug "Checking database schema at $DB_FILE"

    # If the database doesn't exist yet there is nothing to migrate;
    # initialize_database() runs immediately after this and creates it fresh
    # with the new schema.
    [ -f "$DB_FILE" ] || return 0

    # A DB file can exist without the gpu_metrics table (empty file, manually
    # dropped, partial recovery). In that case there is nothing to migrate —
    # initialize_database() runs right after this and will CREATE TABLE IF
    # NOT EXISTS the fresh schema. Without this check, the first ALTER TABLE
    # below would fail and fail-fast would exit the container unnecessarily.
    local table_exists
    table_exists=$(sqlite3 -init /dev/null "$DB_FILE" \
        "SELECT name FROM sqlite_master WHERE type='table' AND name='gpu_metrics';" 2>/dev/null)
    if [ -z "$table_exists" ]; then
        log_debug "gpu_metrics table absent; skipping migration (initialize_database will create fresh schema)"
        return 0
    fi

    # Enable WAL once. WAL persists as a database attribute, so setting it
    # here is equivalent to setting it at database creation. Only log when
    # we actually change the mode — keeps no-op runs quiet.
    #
    # `PRAGMA journal_mode=WAL` always exits 0, but returns whatever mode
    # SQLite actually applied. On filesystems that do not support WAL (tmpfs,
    # some NFS configurations, FAT) it silently falls back to rollback
    # journal mode. We capture the returned mode and warn (not fail) if it
    # is not "wal": losing concurrent-reader scalability is a perf
    # degradation, not a correctness issue — the collector is the only
    # writer and the API's reads are single-row SELECTs that rollback
    # mode handles correctly. Failing hard here would be user-hostile for
    # ephemeral dev deployments on tmpfs.
    local current_mode new_mode
    current_mode=$(sqlite3 -init /dev/null "$DB_FILE" "PRAGMA journal_mode;" 2>/dev/null)
    if [ "$current_mode" != "wal" ]; then
        log_warning "Migrating gpu_metrics: enabling WAL journal mode (was: ${current_mode:-unknown})"
        new_mode=$(sqlite3 -init /dev/null "$DB_FILE" "PRAGMA journal_mode=WAL;" 2>/dev/null) || {
            log_error "Failed to enable WAL journal mode (sqlite3 error)"
            return 1
        }
        if [ "$new_mode" != "wal" ]; then
            log_warning "WAL not supported on this filesystem; SQLite reported '${new_mode:-unknown}'. Concurrent-reader scalability will be reduced but correctness is unaffected."
        fi
    fi

    # Add gpu_index if missing
    if ! gpu_metrics_has_column gpu_index; then
        log_warning "Migrating gpu_metrics: adding gpu_index column (default 0)"
        sqlite3 -init /dev/null "$DB_FILE" \
            "ALTER TABLE gpu_metrics ADD COLUMN gpu_index INTEGER NOT NULL DEFAULT 0;" || {
            log_error "Failed to add gpu_index column"
            return 1
        }
    fi

    # Add gpu_uuid column if missing (separate from backfill so a partial
    # previous failure — ALTER succeeded but UPDATE didn't — can still be
    # recovered by the idempotent backfill below).
    if ! gpu_metrics_has_column gpu_uuid; then
        log_warning "Migrating gpu_metrics: adding gpu_uuid column"
        sqlite3 -init /dev/null "$DB_FILE" \
            "ALTER TABLE gpu_metrics ADD COLUMN gpu_uuid TEXT;" || {
            log_error "Failed to add gpu_uuid column"
            return 1
        }
    fi

    # Backfill any NULL gpu_uuid rows with the current GPU's UUID.
    # This runs on EVERY boot regardless of whether we just added the column,
    # so:
    #   (a) a partial previous migration (ALTER ok, UPDATE failed) recovers;
    #   (b) rows inserted by a bug elsewhere with NULL gpu_uuid get fixed;
    #   (c) the operation is a cheap COUNT-then-UPDATE on steady-state runs
    #       where nothing is NULL.
    local null_count
    null_count=$(sqlite3 -init /dev/null "$DB_FILE" \
        "SELECT COUNT(*) FROM gpu_metrics WHERE gpu_uuid IS NULL;" 2>/dev/null)
    if [ "${null_count:-0}" -gt 0 ]; then
        local current_uuid escaped_uuid
        current_uuid=$(nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | head -n1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -z "$current_uuid" ] && current_uuid="legacy-unknown"

        # SQL-escape single quotes (external command output — UUIDs are
        # well-formed in practice but the escape keeps the pattern safe
        # and robust to future driver changes).
        escaped_uuid=${current_uuid//\'/\'\'}

        log_warning "Backfilling $null_count gpu_metrics row(s) with NULL gpu_uuid → '$current_uuid'"
        sqlite3 -init /dev/null "$DB_FILE" \
            "UPDATE gpu_metrics SET gpu_uuid = '$escaped_uuid' WHERE gpu_uuid IS NULL;" || {
            log_error "Failed to backfill gpu_uuid"
            return 1
        }
    fi

    # Add interval_s if missing. DEFAULT 4 is correct for all existing rows
    # because the pre-overhaul script only ever sampled at 4s.
    if ! gpu_metrics_has_column interval_s; then
        log_warning "Migrating gpu_metrics: adding interval_s column (default 4)"
        sqlite3 -init /dev/null "$DB_FILE" \
            "ALTER TABLE gpu_metrics ADD COLUMN interval_s INTEGER NOT NULL DEFAULT 4;" || {
            log_error "Failed to add interval_s column"
            return 1
        }
    fi

    # Composite index for the (gpu_index, timestamp_epoch) queries Phase 3
    # introduces. IF NOT EXISTS makes this a no-op on re-runs.
    sqlite3 -init /dev/null "$DB_FILE" \
        "CREATE INDEX IF NOT EXISTS idx_gpu_metrics_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);" || {
        log_error "Failed to create composite (gpu_index, timestamp_epoch) index"
        return 1
    }

    log_debug "Schema migration complete"
    return 0
}

###############################################################################
# initialize_database: Creates and initializes the SQLite database
# Handles schema creation and indexes for efficient queries
###############################################################################
function initialize_database() {
    log_debug "Initializing SQLite database at $DB_FILE"
    
    if [ ! -f "$DB_FILE" ]; then
        log_debug "Creating new database file"
        touch "$DB_FILE"
        chmod 666 "$DB_FILE"  # Ensure proper permissions
    fi
    
    # Create SQLite tables and indexes. Fresh databases get the Phase 1
    # schema directly; existing databases are brought forward by
    # migrate_database() which runs just before this function.
    # stdout is redirected because `PRAGMA journal_mode=WAL` returns the
    # resulting mode and would otherwise leak "wal" into container logs.
    sqlite3 -init /dev/null "$DB_FILE" >/dev/null << EOF
    PRAGMA journal_mode=WAL;

    CREATE TABLE IF NOT EXISTS gpu_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        timestamp_epoch INTEGER NOT NULL,
        temperature REAL NOT NULL,
        utilization REAL NOT NULL,
        memory REAL NOT NULL,
        power REAL NOT NULL,
        gpu_index INTEGER NOT NULL DEFAULT 0,
        gpu_uuid TEXT,
        interval_s INTEGER NOT NULL DEFAULT 4
    );

    CREATE INDEX IF NOT EXISTS idx_gpu_metrics_timestamp_epoch ON gpu_metrics(timestamp_epoch);
    CREATE INDEX IF NOT EXISTS idx_gpu_metrics_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);
    
    -- Create a view for the legacy JSON format to maintain compatibility
    CREATE VIEW IF NOT EXISTS history_json_view AS
    SELECT 
        json_object(
            'timestamps', json_group_array(timestamp),
            'temperatures', json_group_array(temperature),
            'utilizations', json_group_array(utilization),
            'memory', json_group_array(memory),
            'power', json_group_array(power)
        ) AS json_data
    FROM (
        SELECT timestamp, temperature, utilization, memory, power
        FROM gpu_metrics
        WHERE timestamp_epoch > (strftime('%s', 'now') - 86400)
        ORDER BY timestamp_epoch ASC
    );
EOF
    
    if [ $? -ne 0 ]; then
        log_error "Failed to initialize SQLite database"
        return 1
    fi
    
    log_debug "Database initialized successfully"
    return 0
}

###############################################################################
# NOTE (Phase 3): process_historical_data and process_24hr_stats used to live
# here. They exported SQLite data to legacy flat files (history.json,
# gpu_24hr_stats.txt) via embedded-Python heredocs, for the pre-API frontend
# to consume via static fetch(). Phase 3 deletes them in favour of
# /api/metrics/history and /api/stats/24h, which query the DB directly via
# aiohttp + sqlite3 in server.py. The collector still writes
# gpu_current_stats.json as a transitional shim for the retrofitted
# legacy frontend until Phase 4's full UI rewrite deletes that too.
###############################################################################
###############################################################################
# rotate_logs: Manages log file sizes and retention
# Rotates logs based on:
# - Size limit (5MB)
# - Age limit (25 hr)
# Handles: error.log, warning.log, gpu_stats.log
###############################################################################
rotate_logs() {
    # Phase 6c: Read size + age thresholds live from settings.json
    # every invocation. The jq filter falls back to the Phase-1
    # defaults (5 MB / 25 h) if the file is missing, malformed, or
    # the keys are absent — matching the pre-Phase-1 hardcoded
    # behaviour exactly. A user who changes logging.max_size_mb or
    # logging.max_age_hours in the Settings view sees the new
    # values take effect on the very next rotate_logs call (which
    # the main loop fires hourly).
    local max_size_mb=5
    local max_age_hours=25
    if [ -r "$SETTINGS_FILE" ] && command -v jq >/dev/null 2>&1; then
        local s
        s=$(jq -r '.logging.max_size_mb // 5' "$SETTINGS_FILE" 2>/dev/null)
        [[ "$s" =~ ^[0-9]+$ ]] && [ "$s" -ge 1 ] && [ "$s" -le 100 ] && max_size_mb=$s
        local a
        a=$(jq -r '.logging.max_age_hours // 25' "$SETTINGS_FILE" 2>/dev/null)
        [[ "$a" =~ ^[0-9]+$ ]] && [ "$a" -ge 1 ] && [ "$a" -le 720 ] && max_age_hours=$a
    fi

    local max_size=$(( max_size_mb * 1024 * 1024 ))
    local max_age=$(( max_age_hours * 3600 ))
    local current_time=$(date +%s)

    rotate_log_file() {
        local log_file=$1
        local timestamp=$(date '+%Y%m%d-%H%M%S')
        
        # Size-based rotation
        if [[ -f "$log_file" && $(stat -f%z "$log_file" 2>/dev/null || stat -c%s "$log_file") -gt $max_size ]]; then
            mv "$log_file" "${log_file}.${timestamp}"
            touch "$log_file"
            log_debug "Rotated $log_file due to size"
        fi

        # Age-based cleanup
        find "$(dirname "$log_file")" -name "$(basename "$log_file").*" -type f | while read rotated_log; do
            local file_time=$(stat -f%m "$rotated_log" 2>/dev/null || stat -c%Y "$rotated_log")
            if (( current_time - file_time > max_age )); then
                rm "$rotated_log"
                log_debug "Removed old log: $rotated_log"
            fi
        done
    }

    # Rotate error and warning logs
    rotate_log_file "$ERROR_LOG"
    rotate_log_file "$WARNING_LOG"
    rotate_log_file "$LOG_FILE"
}

###############################################################################
# clean_old_data: Purges old data from SQLite database
# Ensures database doesn't grow indefinitely while maintaining performance
###############################################################################
function clean_old_data() {
    log_debug "Cleaning old data from SQLite database"

    # Phase 6c: Read housekeeping.retention_days live from
    # settings.json every invocation, falling back to the Phase-1
    # RETENTION_SECONDS default (3 days + 10 minute slack) on
    # missing / malformed / out-of-range. Same jq-with-validation
    # pattern as load_settings and the new rotate_logs block above.
    # A user who changes retention in the Settings view sees it
    # take effect on the next clean_old_data call (fired once per
    # day).
    #
    # The +600 slack preserves the exact pre-Phase-1 behavior: the
    # original RETENTION_SECONDS constant was $((3*86400+600)),
    # ten minutes past the 3-day boundary, so a row sampled at
    # "now - 3 days - 5 minutes" stayed visible in the last-24h
    # chart window until the next daily sweep. Dropping the slack
    # would subtly shrink the effective retention by 10 minutes —
    # imperceptible in practice but explicitly not what the
    # Phase-1-compatibility comment promised.
    local retention_days=3
    local retention_slack=600
    if [ -r "$SETTINGS_FILE" ] && command -v jq >/dev/null 2>&1; then
        local r
        r=$(jq -r '.housekeeping.retention_days // 3' "$SETTINGS_FILE" 2>/dev/null)
        [[ "$r" =~ ^[0-9]+$ ]] && [ "$r" -ge 1 ] && [ "$r" -le 365 ] && retention_days=$r
    fi
    local retention_seconds=$(( retention_days * 86400 + retention_slack ))
    local cutoff_time=$(( $(date +%s) - retention_seconds ))

    sqlite3 "$DB_FILE" <<EOF
    DELETE FROM gpu_metrics WHERE timestamp_epoch < $cutoff_time;
    VACUUM; -- Free up disk space and optimize
EOF
    
    if [ $? -ne 0 ]; then
        log_error "Failed to clean old data from database"
        return 1
    fi
    
    log_debug "Old data cleaned successfully"
    return 0
}

###############################################################################
# safe_write_json: Safely writes JSON data to prevent corruption
# Arguments:
#   $1 - Target file path
#   $2 - JSON content to write
# Returns:
#   0 on success, 1 on failure
###############################################################################
function safe_write_json() {
    local file="$1"
    local content="$2"
    local temp="${file}.tmp"
    local backup="${file}.bak"
    
    # Write to temp file
    echo "$content" > "$temp"
    
    # Verify temp file was written successfully
    if [ -s "$temp" ]; then
        # Create backup of current file if it exists
        [ -f "$file" ] && cp "$file" "$backup"
        
        # Atomic move of temp to real file
        mv "$temp" "$file"
        
        # Clean up backup if everything succeeded
        [ -f "$backup" ] && rm "$backup"
        
        return 0
    else
        log_error "Failed to write to temp file: $temp"
        # Restore from backup if available
        [ -f "$backup" ] && mv "$backup" "$file"
        return 1
    fi
}

###############################################################################
# process_buffer: Safely handles buffered GPU metrics data
# Implements atomic write operations to prevent data loss during system updates
#
# Retry semantics (task #23):
#
#   On python insert failure, the buffered rows are renamed from
#   $temp_file → ${BUFFER_FILE}.pending, and a sibling
#   ${BUFFER_FILE}.pending.retries file records the consecutive
#   failure count. On the next process_buffer call, any existing
#   .pending file is CHRONOLOGICALLY PREPENDED to the freshly-copied
#   buffer before python runs — so the retry and the new rows are
#   written as a single transaction, preserving sample ordering.
#
#   After MAX_PROCESS_BUFFER_RETRIES consecutive failures, the pending
#   file is renamed to ${BUFFER_FILE}.stuck-YYYYMMDDHHMMSS for manual
#   operator recovery, and the retry count resets. This prevents a
#   persistent bug (e.g. schema mismatch, read-only filesystem) from
#   infinitely accumulating unprocessable rows in .pending.
#
#   Regardless of python success/failure, every row is ALSO appended
#   to $LOG_FILE as an audit trail — that safety net was added in
#   Phase 1 and is preserved here.
#
# Returns: 0 on success, 1 on failure
###############################################################################
MAX_PROCESS_BUFFER_RETRIES=5

function process_buffer() {
    local temp_file="${BUFFER_FILE}.tmp"
    local pending_file="${BUFFER_FILE}.pending"
    local retries_file="${pending_file}.retries"
    local success=0

    # Create temp file with buffer contents
    if cp "$BUFFER_FILE" "$temp_file"; then
        # Clear original buffer only after successful copy so any
        # samples written between now and python exit go into a clean
        # slate rather than being double-processed or lost.
        > "$BUFFER_FILE"

        # If a prior tick left a .pending file behind, prepend its
        # contents to the freshly-copied buffer so old-then-new
        # chronological order is preserved in the retry transaction.
        # Rows in .pending are always older than anything now sitting
        # in $temp_file because the main loop is serialized — no
        # concurrent writer could have added to .pending after we
        # started this function. Use a merge temp file to avoid the
        # "cat a > a" race that would silently produce an empty file.
        local current_retries=0
        if [ -f "$pending_file" ]; then
            local merge_tmp="${temp_file}.merge"
            if cat "$pending_file" "$temp_file" > "$merge_tmp" 2>/dev/null; then
                mv "$merge_tmp" "$temp_file"
            else
                log_warning "process_buffer: could not merge .pending (continuing with fresh buffer only)"
                rm -f "$merge_tmp"
            fi
            # Read the retry counter — missing or malformed counter
            # resets to 0, which is the conservative default (worst
            # case: one extra retry cycle before escalation to .stuck).
            if [ -f "$retries_file" ]; then
                current_retries=$(cat "$retries_file" 2>/dev/null || echo 0)
                [[ "$current_retries" =~ ^[0-9]+$ ]] || current_retries=0
            fi
        fi
        
        # Process buffer with Python and write to database
        cat > /tmp/process_buffer.py << 'PYTHONSCRIPT'
import os
import sys
import sqlite3
from datetime import datetime

def load_gpu_uuid_by_index(inventory_path):
    """Load a map of gpu_index (int) -> gpu_uuid (str) from gpu_inventory.json.

    Returns an empty dict if the file is missing, unreadable, or malformed.
    Callers fall back to the GPU_MONITOR_GPU_UUID env var for single-GPU
    deployments where the inventory file somehow doesn't exist yet."""
    try:
        import json
        with open(inventory_path, 'r') as f:
            inv = json.load(f)
        return {int(g['index']): g['uuid'] for g in inv.get('gpus', [])}
    except (IOError, ValueError, KeyError, TypeError):
        return {}


def _safe_float(s, default=0.0):
    """Parse a CSV cell to float, returning `default` for N/A-style values
    or any non-numeric input. nvidia-smi emits '[Not Supported]' and 'N/A'
    for missing-telemetry fields (compute-only GPUs, vGPUs, older cards
    where the sensor isn't wired up), and a bare float() call on those
    aborts the whole buffer flush transaction, losing every row."""
    try:
        if s is None:
            return default
        cleaned = s.strip()
        if cleaned in ('N/A', '[N/A]', '[Not Supported]', ''):
            return default
        return float(cleaned)
    except (ValueError, AttributeError):
        return default


def process_buffer(db_path, buffer_lines):
    # interval_s (the cadence the collector is currently running at) comes
    # from the GPU_MONITOR_INTERVAL_S env var set by the bash caller.
    # gpu_uuid is looked up per-row from the gpu_inventory.json file that
    # discover_gpus() writes at startup. Two distinct fallback modes:
    #
    #   1. Inventory file is empty/unreadable (single-GPU dev env without
    #      nvidia-smi): use GPU_MONITOR_GPU_UUID env var for every row.
    #      This is the expected legacy single-GPU path.
    #
    #   2. Inventory file has data but is missing a specific gpu_index
    #      (hot-add/remove after startup, partial corruption): use the
    #      'legacy-unknown' sentinel rather than silently attributing the
    #      row to some other GPU's UUID. Silent misattribution would
    #      corrupt per-GPU analytics in a way that is hard to detect later.
    try:
        interval_s = int(os.environ.get('GPU_MONITOR_INTERVAL_S', '4'))
    except ValueError:
        interval_s = 4
    legacy_env_uuid = os.environ.get('GPU_MONITOR_GPU_UUID', 'legacy-unknown') or 'legacy-unknown'
    uuid_by_index = load_gpu_uuid_by_index('/app/gpu_inventory.json')
    inventory_empty = not uuid_by_index

    try:
        conn = sqlite3.connect(db_path)
        conn.execute('BEGIN TRANSACTION')

        # Phase 2 insert: gpu_index is per-row (from the buffer line),
        # gpu_uuid is looked up from the inventory, interval_s is the
        # current collector cadence.
        stmt = '''
            INSERT INTO gpu_metrics
            (timestamp, timestamp_epoch, temperature, utilization, memory, power,
             gpu_index, gpu_uuid, interval_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        '''

        for line in buffer_lines:
            line = line.strip()
            if not line:
                continue

            parts = line.split(',')
            # Two supported formats:
            #   Phase 2 (6 fields): timestamp,gpu_index,temp,util,mem,power
            #   Phase 1 (5 fields): timestamp,temp,util,mem,power — legacy
            # The 5-field path keeps a pre-upgrade on-disk buffer readable
            # even after the bash script swaps to the 6-field format.
            if len(parts) == 6:
                timestamp = parts[0]
                try:
                    gpu_index = int(parts[1])
                except ValueError:
                    gpu_index = 0
                metric_parts = parts[2:]
            elif len(parts) == 5:
                timestamp = parts[0]
                gpu_index = 0
                metric_parts = parts[1:]
            else:
                continue

            # Use _safe_float for every metric field. Previously only
            # power had N/A-aware handling, so a temperature/utilization/
            # memory field of 'N/A' or '[Not Supported]' would raise
            # ValueError out of float() and abort the whole flush,
            # losing all buffered rows. Now one bad reading defaults to
            # 0 and the flush proceeds — partial data is better than no
            # data for a homelab GPU monitor.
            temperature = _safe_float(metric_parts[0])
            utilization = _safe_float(metric_parts[1])
            memory = _safe_float(metric_parts[2])
            power = _safe_float(metric_parts[3])

            # Parse timestamp to epoch. New format is "%Y-%m-%d %H:%M:%S";
            # fall back to the legacy yearless format for any stragglers in an
            # on-disk buffer from a pre-upgrade container.
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                current_year = datetime.now().year
                dt = datetime.strptime(f"{current_year} {timestamp}", "%Y %m-%d %H:%M:%S")
            timestamp_epoch = int(dt.timestamp())

            # Dual-mode fallback per the comment at the top of this
            # function. Three distinct cases:
            #
            #   (a) inventory present with an entry for this gpu_index
            #       → use the real UUID from the inventory.
            #   (b) inventory present but missing this gpu_index → use
            #       'legacy-unknown' sentinel (partial corruption case).
            #   (c) inventory empty / unreadable:
            #       - gpu_index == 0 → env var (single-GPU legacy path)
            #       - gpu_index != 0 → 'legacy-unknown' sentinel,
            #         NOT the env var. The env var is specifically GPU 0's
            #         UUID, so using it for non-zero indexes would silently
            #         misattribute their rows to GPU 0 — exactly the
            #         data-integrity issue we fixed in the (b) branch.
            if inventory_empty:
                gpu_uuid = legacy_env_uuid if gpu_index == 0 else 'legacy-unknown'
            else:
                gpu_uuid = uuid_by_index.get(gpu_index, 'legacy-unknown')

            conn.execute(stmt,
                (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                 gpu_index, gpu_uuid, interval_s))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Error processing buffer: {e}", file=sys.stderr)
        if 'conn' in locals():
            conn.rollback()
        return False
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <db_path>", file=sys.stderr)
        sys.exit(1)
        
    buffer_lines = sys.stdin.readlines()
    success = process_buffer(sys.argv[1], buffer_lines)
    sys.exit(0 if success else 1)
PYTHONSCRIPT

        # Execute the script with buffer data. GPU_MONITOR_INTERVAL_S and
        # GPU_MONITOR_GPU_UUID are read by process_buffer.py and written
        # into every inserted row so that Phase 5's power integration stays
        # correct across interval changes and multi-GPU installs.
        if cat "$temp_file" \
            | GPU_MONITOR_INTERVAL_S="$INTERVAL" \
              GPU_MONITOR_GPU_UUID="${GPU_UUID:-legacy-unknown}" \
              python3 /tmp/process_buffer.py "$DB_FILE"; then
            log_debug "Successfully processed buffer data into database"
            success=1
        else
            log_error "Failed to process buffer into database"
        fi
        # Append buffered samples to the audit log REGARDLESS of DB insert
        # success. The audit-log safety net was added in Phase 1 so every
        # sample is recoverable from $LOG_FILE even if both the DB write
        # and the retry path fail. The retry path (below) stashes failed
        # rows to .pending / .stuck-* for programmatic recovery; the audit
        # log is the belt-and-braces fallback for the case where even the
        # retry path is broken (read-only filesystem, etc.).
        cat "$temp_file" >> "$LOG_FILE"

        # Clean up the Python script (recreated on every call)
        rm -f /tmp/process_buffer.py
    else
        log_error "Failed to create temp buffer file"
    fi

    # Final disposition of $temp_file depends on whether the Python
    # insert succeeded and whether we've exhausted the retry budget:
    if [ "$success" -eq 1 ]; then
        # Clean slate: delete temp + any lingering .pending retry
        # artifacts. A successful insert discharges all buffered rows
        # — the .pending file (which was merged into temp_file at the
        # top of this function) has already been committed to the DB.
        rm -f "$temp_file" "$pending_file" "$retries_file"
    elif [ -f "$temp_file" ]; then
        # Python failed. Decide between "defer for retry" and
        # "escalate to .stuck for manual recovery".
        local next_retries=$((current_retries + 1))
        if [ "$next_retries" -ge "$MAX_PROCESS_BUFFER_RETRIES" ]; then
            # Exhausted retry budget — stash to .stuck-* with a
            # timestamp suffix and reset the retry state. The operator
            # needs to look at the file manually (fix the underlying
            # issue, replay via sqlite3 + the process_buffer.py heredoc,
            # or delete if unrecoverable). Logging an ERROR here flags
            # it in the main log stream so it surfaces in Settings →
            # Housekeeping → DB info for observability.
            local stuck_file
            stuck_file="${BUFFER_FILE}.stuck-$(date '+%Y%m%d%H%M%S')"
            if mv "$temp_file" "$stuck_file" 2>/dev/null; then
                rm -f "$pending_file" "$retries_file"
                log_error "process_buffer: $next_retries consecutive DB-insert failures; stashed $(wc -l < "$stuck_file" 2>/dev/null || echo '?') rows to $stuck_file for manual recovery"
            else
                log_error "process_buffer: cannot escalate to .stuck-* (mv failed); keeping .pending"
                # Leave .pending/temp_file as-is — next tick will try
                # again. Better to spin on retries than to silently drop.
                mv "$temp_file" "$pending_file" 2>/dev/null || true
            fi
        else
            # Still within retry budget — defer to .pending for the
            # next tick to pick up and prepend.
            if mv "$temp_file" "$pending_file" 2>/dev/null; then
                echo "$next_retries" > "$retries_file" 2>/dev/null || \
                    log_warning "process_buffer: could not write retry counter (next tick may reset)"
                log_warning "process_buffer: DB insert failed (retry $next_retries/$MAX_PROCESS_BUFFER_RETRIES); deferred to $pending_file"
            else
                log_error "process_buffer: cannot rename temp to .pending; rows will be re-fetched on audit-log replay only"
                rm -f "$temp_file"
            fi
        fi
    fi

    # Return result
    return $((1 - success))
}

###############################################################################
# update_stats: Core function for GPU metrics collection and processing
# Collects GPU metrics every INTERVAL seconds and manages data flow
# Handles:
# - GPU metric collection via nvidia-smi
# - Buffer management
# - JSON updates for real-time display
# - Error recovery for system updates and GPU access issues
# Returns: 0 on success, 1 on failure
###############################################################################
update_stats() {
    local write_failed=0

    # Pick up any live changes to collection.interval_seconds /
    # collection.flush_interval_seconds from settings.json. No-op if the
    # file is missing or unchanged. See load_settings() definition for
    # validation range and fallback behaviour.
    load_settings

    # Collect current GPU metrics.
    # Timestamp includes the year to avoid the year-rollover bug where buffered
    # records were misassigned to the flush-time year (e.g. a Dec 31 23:59 reading
    # flushed at Jan 1 00:00 would land an entire year in the future).
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    # Multi-GPU query: one row per attached GPU, prefixed with the GPU index
    # so rows can be disambiguated downstream. The `index` field is always
    # emitted first and the remaining field order matches Phase 1.
    #
    # When NVIDIA_SMI_AVAILABLE=0 (set at startup by the cache in the
    # startup block), skip the nvidia-smi call entirely and synthesize
    # zeroed rows for every discovered GPU. This keeps dev/CI
    # environments quiet without false-alarm ERROR lines or 3× retry
    # delays, while preserving the transient-failure retry path for
    # real production hardware where nvidia-smi IS installed but
    # occasionally hiccups.
    local gpu_stats
    if [ "$NVIDIA_SMI_AVAILABLE" -eq 1 ]; then
        gpu_stats=$(nvidia-smi \
            --query-gpu=index,temperature.gpu,utilization.gpu,memory.used,power.draw \
            --format=csv,noheader,nounits 2>/dev/null)

        if [[ -z "$gpu_stats" ]]; then
            # Return 1 so the main retry loop engages its nvidia-smi-hiccup
            # backoff (3 × 1s retries). Phase 2 initially returned 0 here
            # which silently suppressed the retry mechanism and contradicted
            # the "Returns: 0 on success, 1 on failure" contract.
            log_error "Failed to get GPU stats output"
            return 1
        fi
    else
        # Synthetic path: build a multi-line CSV that matches nvidia-smi's
        # format ("index, temperature, util, mem, power" — one row per
        # discovered GPU). Values are zero across the board so the buffer
        # parsing loop below can process them through the exact same code
        # path as the real-hardware case — no alternate insert path, no
        # special-case in process_buffer, no divergence in downstream
        # aggregations. The Phase 5 power integration correctly
        # contributes 0 Wh per tick from these rows, and /api/stats/power
        # already surfaces an `insufficient_telemetry: true` flag when
        # the window has zero power samples, so the UI won't claim
        # fake readings are real.
        gpu_stats=""
        local i
        for i in "${GPU_INDEXES[@]}"; do
            if [ -n "$gpu_stats" ]; then
                gpu_stats+=$'\n'
            fi
            gpu_stats+="${i}, 0, 0, 0, 0"
        done
        if [ -z "$gpu_stats" ]; then
            # Extremely defensive: discover_gpus always populates at least
            # one synthetic entry, so GPU_INDEXES should never be empty
            # here. If it somehow is, fall through to a single GPU 0 row
            # rather than silently no-op'ing the tick.
            gpu_stats="0, 0, 0, 0, 0"
        fi
    fi

    # Verify buffer write access before proceeding
    if ! touch "$BUFFER_FILE" 2>/dev/null; then
        log_error "Cannot write to buffer file"
        return 1
    fi

    # Parse each row into the Phase 2 6-field buffer format:
    #   timestamp,gpu_index,temperature,utilization,memory,power
    # Phase 4 deleted the gpu_current_stats.json shim — the new
    # frontend reads /api/metrics/current directly from the DB, so
    # the per-row capture of GPU 0's values is no longer needed.
    local row_count=0
    while IFS=',' read -r idx temp util mem power; do
        # _trim_ws is a top-level helper (defined earlier in this file).
        # Defining it inside this function would redefine a GLOBAL bash
        # function on every tick — bash functions aren't locally scoped,
        # regardless of where they're declared.
        _trim_ws "$idx";   idx="$REPLY"
        _trim_ws "$temp";  temp="$REPLY"
        _trim_ws "$util";  util="$REPLY"
        _trim_ws "$mem";   mem="$REPLY"
        # power has additional [N/A] bracket handling; strip spaces and
        # brackets using parameter-expansion substitutions.
        power="${power// /}"
        power="${power//[/}"
        power="${power//]/}"

        [ -z "$idx" ] && continue

        # Handle [N/A] power per-row; some laptop/dGPU combos don't report
        # power telemetry at all. Because the three stripping lines above
        # have already removed the `[` and `]` characters, only the
        # post-strip "N/A" (or empty) form is reachable — an earlier
        # version of this branch also checked `"$power" == "[N/A]"`,
        # which was unreachable dead code.
        if [[ "$power" == "N/A" || -z "$power" ]]; then
            power="0"
        fi

        # Append 6-field buffer line (atomic single-line append is
        # write-safe across concurrent ticks because each update_stats
        # call runs to completion before the next `sleep $INTERVAL`.)
        if ! echo "$timestamp,$idx,$temp,$util,$mem,$power" >> "$BUFFER_FILE"; then
            log_error "Failed to write GPU $idx line to buffer"
            write_failed=1
        fi
        row_count=$(( row_count + 1 ))
    done <<< "$gpu_stats"

    # Sanity-check: did nvidia-smi return as many rows as the inventory
    # discovered at startup? A mismatch in EITHER direction suggests a
    # hot-add (row_count > NUM_GPUS — new GPU appeared) or hot-remove
    # (row_count < NUM_GPUS — a GPU vanished, probably a driver/power
    # event). Both directions are worth surfacing so the user notices
    # that the inventory is stale, even though partial data is better
    # than no data for homelab monitoring so we don't fail the tick.
    # The direction is encoded in the log message so operators can
    # distinguish add from remove without re-checking nvidia-smi.
    if [ "$row_count" -ne "${NUM_GPUS:-1}" ]; then
        local direction="unchanged"
        if [ "$row_count" -lt "${NUM_GPUS:-1}" ]; then
            direction="hot-remove suspected"
        elif [ "$row_count" -gt "${NUM_GPUS:-1}" ]; then
            direction="hot-add suspected"
        fi
        log_warning "nvidia-smi returned $row_count rows but discover_gpus found $NUM_GPUS GPUs at startup ($direction); restart the container to refresh the inventory"
    fi

    # Detailed error logging for debugging any write failures.
    # log_error reads its message from $1, not stdin, so we capture each
    # diagnostic command's output via $(...) and pass as an argument.
    if [[ $write_failed -eq 1 ]]; then
        log_error "Buffer write details:"
        log_error "$(ls -l "$BUFFER_FILE" 2>&1)"
        log_error "$(df -h "$(dirname "$BUFFER_FILE")" 2>&1)"
    fi

    # Process buffer when full. BUFFER_SIZE is already scaled by NUM_GPUS
    # in load_settings(), so this threshold means "~flush_interval_seconds
    # of wall-clock data buffered" regardless of card count. Phase 3:
    # the historical/24h flat-file exporters are gone; process_buffer is
    # now the only thing that runs at a buffer-full boundary. The API
    # routes in server.py query the DB directly on every request.
    if [[ -f "$BUFFER_FILE" ]] && [[ $(wc -l < "$BUFFER_FILE") -ge $BUFFER_SIZE ]]; then
        process_buffer
    fi
}

# jq is a hard dependency for the startup path: discover_gpus and the
# gpu_config.json generation both use it. Fail fast with a clear error.
if ! command -v jq >/dev/null 2>&1; then
    log_error "jq is required to generate $CONFIG_FILE but was not found in PATH"
    exit 1
fi

# Cache nvidia-smi availability once at startup. See the comment on the
# NVIDIA_SMI_AVAILABLE declaration for why this matters — short version:
# dev environments without nvidia-smi shouldn't flood the logs with
# per-tick nvidia-smi-hiccup retries for a failure that will never
# recover. If the binary is missing, log a single synthetic-mode
# warning at startup and let update_stats generate zeroed rows for
# every tick. discover_gpus already has its own synthetic fallback
# for the inventory side; this flag just extends that behavior to the
# hot-path collector loop.
if command -v nvidia-smi >/dev/null 2>&1; then
    NVIDIA_SMI_AVAILABLE=1
else
    NVIDIA_SMI_AVAILABLE=0
    log_warning "nvidia-smi not found in PATH — running in synthetic-metrics mode"
    log_warning "All telemetry rows will be zeroed; this is fine for test/CI but"
    log_warning "indicates a missing NVIDIA driver if you're running on real hardware"
fi

# Discover attached GPUs. Populates NUM_GPUS, GPU_INDEXES, GPU_NAMES,
# GPU_UUIDS, and writes $INVENTORY_FILE atomically via safe_write_json.
# Falls back to a synthetic single-GPU entry if nvidia-smi is unavailable,
# so the rest of the pipeline stays uniform for test and dev environments.
# Fail-fast on inventory-write failure: running without a valid inventory
# file would silently degrade multi-GPU lookups to the single-UUID env
# var fallback, which is much harder to diagnose than a clean startup
# error.
if ! discover_gpus; then
    log_error "Failed to discover GPUs; refusing to start without a valid inventory"
    exit 1
fi

# Write gpu_config.json using jq for proper JSON escaping (GPU_NAME from
# nvidia-smi and GPU_MONITOR_VERSION from the VERSION file are both
# external-ish inputs that could in principle contain JSON-significant
# characters; jq handles escaping per RFC 8259). The legacy "gpu_name"
# top-level key is preserved as gpus[0].name so the existing frontend
# keeps working unchanged during the Phase 2 → Phase 3 transition.
# The new "gpus" array carries the full multi-GPU inventory by slurping
# $INVENTORY_FILE (which discover_gpus just wrote) and projecting .gpus.
if ! CONFIG_JSON=$(jq -n \
    --arg gpu_name "$GPU_NAME" \
    --arg version "$GPU_MONITOR_VERSION" \
    --slurpfile inv "$INVENTORY_FILE" \
    '{gpu_name: $gpu_name, version: $version, gpus: $inv[0].gpus}'); then
    log_error "Failed to generate $CONFIG_FILE via jq"
    exit 1
fi
safe_write_json "$CONFIG_FILE" "$CONFIG_JSON"

# Run schema migrations on the existing DB (no-op on fresh installs), then
# initialize / open the database for writes. Migration must run BEFORE
# initialize_database so CREATE TABLE IF NOT EXISTS does not race the ALTER.
#
# Fail-fast on migration error: a half-migrated schema would leave the
# collector silently writing to a DB that process_buffer.py can no longer
# INSERT into, producing an infinite error loop with no clear signal.
# Exiting here lets the container supervisor surface the error and restart.
if ! migrate_database; then
    log_error "Database migration failed; refusing to start in a half-migrated state"
    exit 1
fi
initialize_database
# Pick up any user-defined collection cadence from settings.json on startup
# so the very first tick honours it (later ticks re-read each iteration).
load_settings

###############################################################################
# run_web_server: Runs server.py in a supervised respawn loop.
# If aiohttp dies, the bash collector would otherwise keep running silently
# while the dashboard went dark. This wrapper logs the exit and relaunches.
###############################################################################
run_web_server() {
    cd /app
    while true; do
        python3 server.py
        local rc=$?
        log_error "server.py exited with code $rc; respawning in 2s"
        sleep 2
    done
}

run_web_server &
SERVER_PID=$!
log_debug "Web server supervisor started (pid=$SERVER_PID)"

###############################################################################
# run_report_scheduler: Runs reporting/scheduler.py in a supervised respawn
# loop. The scheduler is a standalone asyncio loop that wakes every 60s,
# reads settings.json, fires any due report schedules, and sleeps again.
# If it dies (unhandled exception, disk full, etc.), this wrapper logs the
# exit and relaunches after a 2s backoff — same pattern as run_web_server.
#
# The scheduler subprocess shares settings.json and the SQLite DB with the
# collector and the server; all three talk through files, not shared
# memory, so crash isolation is trivial. WAL mode (from Phase 1) lets all
# three coexist as concurrent readers + one writer (the collector).
#
# Skipped entirely if reporting/scheduler.py is missing — allows users to
# ship a pared-down image without the matplotlib/jinja/premailer deps by
# deleting the reporting/ subtree from their custom build.
###############################################################################
run_report_scheduler() {
    cd /app
    while true; do
        python3 reporting/scheduler.py
        local rc=$?
        log_warning "reporting/scheduler.py exited with code $rc; respawning in 2s"
        sleep 2
    done
}

if [ -f /app/reporting/scheduler.py ]; then
    run_report_scheduler &
    SCHEDULER_PID=$!
    log_debug "Report scheduler supervisor started (pid=$SCHEDULER_PID)"
else
    log_debug "reporting/scheduler.py not present; skipping scheduler launch"
fi

###############################################################################
# run_alert_checker: Runs reporting/alert_checker.py in a supervised respawn
# loop. The alert checker wakes every poll_interval_seconds (default 30s),
# reads latest GPU metrics from SQLite, evaluates thresholds, and dispatches
# push notifications (ntfy, Pushover, webhook, email) on breach. Shares the
# same crash-resilience pattern as run_report_scheduler and run_web_server.
#
# Skipped if reporting/alert_checker.py is missing — same pared-down-image
# escape hatch as the scheduler.
###############################################################################
run_alert_checker() {
    cd /app
    while true; do
        python3 reporting/alert_checker.py
        local rc=$?
        log_warning "reporting/alert_checker.py exited with code $rc; respawning in 2s"
        sleep 2
    done
}

if [ -f /app/reporting/alert_checker.py ]; then
    run_alert_checker &
    ALERT_CHECKER_PID=$!
    log_debug "Alert checker supervisor started (pid=$ALERT_CHECKER_PID)"
else
    log_debug "reporting/alert_checker.py not present; skipping alert checker launch"
fi

###############################################################################
# Main Process Loop
# Manages the continuous monitoring process with:
# - Retry mechanism for failed updates
# - Hourly log rotation
# - Error resilience during system updates
###############################################################################
while true; do
    # Update tracking with retry mechanism
    update_success=0
    max_retries=3
    retry_count=0

    # Retry loop for failed updates
    while [ $update_success -eq 0 ] && [ $retry_count -lt $max_retries ]; do
        if update_stats; then
            update_success=1
        else
            retry_count=$((retry_count + 1))
            log_warning "Update failed, attempt $retry_count of $max_retries"
            sleep 1
        fi
    done

    # Handle complete update failure
    if [ $update_success -eq 0 ]; then
        log_error "Multiple update attempts failed, continuing to next cycle"
    fi
    
    # Hourly log rotation and nightly history cleanup
    if [ $(date +%M) -eq 0 ]; then
        rotate_logs
        
        # Clean old database data every hour to keep the DB lean
        if [ $(date +%H) -eq 0 ]; then
            clean_old_data
        fi
    fi
    
    sleep $INTERVAL
done