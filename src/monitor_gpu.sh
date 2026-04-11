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
STATS_FILE="$BASE_DIR/gpu_24hr_stats.txt"
JSON_FILE="$BASE_DIR/gpu_current_stats.json"
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
# Used by clean_old_data (DB purge) AND by export_history_json (JSON export
# cutoff). Keep them in lockstep by deriving both from this one value.
# Default: 3 days + 10 minutes of slack to avoid chart gaps at the edge.
# Phase 6 will source this from housekeeping.retention_days in settings.json.
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

# Get GPU name + UUID once at startup. UUID is stable across reboots and is
# the audit identifier written into every metrics row via the process_buffer
# heredoc. A missing UUID (e.g. no GPU available in the test harness) falls
# back to "legacy-unknown" so rows still satisfy the NOT NULL chain.
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' || echo "GPU")
GPU_UUID=$(nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | head -n1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
[ -z "$GPU_UUID" ] && GPU_UUID="legacy-unknown"
export GPU_UUID
CONFIG_FILE="$BASE_DIR/gpu_config.json"

# Create config JSON with GPU name and application version. Phase 2 will
# extend this to carry a "gpus" array for multi-GPU inventory; for now the
# single "gpu_name" key is preserved for the existing frontend.
cat > "$CONFIG_FILE" << EOF
{
    "gpu_name": "${GPU_NAME}",
    "version": "${GPU_MONITOR_VERSION}"
}
EOF

###############################################################################
# load_settings: Re-reads collection-cadence settings from settings.json and
# applies any changes to INTERVAL / FLUSH_INTERVAL / BUFFER_SIZE. Called once
# per collector tick so the user can change cadence live from the Settings UI
# without restarting the container (propagation delay: at most one tick).
#
# Behaviour if settings.json is missing or malformed: silently keep the
# current in-memory defaults. This keeps first-run containers behaving
# identically to the pre-overhaul script (INTERVAL=4, FLUSH_INTERVAL=60,
# BUFFER_SIZE=15).
#
# Validation: interval_seconds must be in [2, 300]; flush_interval_seconds
# must be in [5, 3600]; anything outside falls back to the default. BUFFER_SIZE
# is derived as ceil(FLUSH_INTERVAL / INTERVAL) with a minimum of 1.
#
# Only logs a message when a value actually changes — keeps the logs quiet
# in the steady state.
###############################################################################
function load_settings() {
    [ -r "$SETTINGS_FILE" ] || return 0
    command -v jq >/dev/null 2>&1 || return 0

    local new_interval new_flush
    new_interval=$(jq -r '.collection.interval_seconds // empty' "$SETTINGS_FILE" 2>/dev/null)
    new_flush=$(jq -r '.collection.flush_interval_seconds // empty' "$SETTINGS_FILE" 2>/dev/null)

    # Validate interval_seconds ∈ [2, 300]; fall back to default otherwise.
    if ! [[ "$new_interval" =~ ^[0-9]+$ ]] || [ "$new_interval" -lt 2 ] || [ "$new_interval" -gt 300 ]; then
        new_interval=4
    fi
    # Validate flush_interval_seconds ∈ [5, 3600]; fall back to default otherwise.
    if ! [[ "$new_flush" =~ ^[0-9]+$ ]] || [ "$new_flush" -lt 5 ] || [ "$new_flush" -gt 3600 ]; then
        new_flush=60
    fi

    if [ "$new_interval" != "$INTERVAL" ] || [ "$new_flush" != "$FLUSH_INTERVAL" ]; then
        INTERVAL="$new_interval"
        FLUSH_INTERVAL="$new_flush"
        BUFFER_SIZE=$(( (FLUSH_INTERVAL + INTERVAL - 1) / INTERVAL ))
        [ "$BUFFER_SIZE" -lt 1 ] && BUFFER_SIZE=1
        log_warning "Collection settings reloaded: interval=${INTERVAL}s flush=${FLUSH_INTERVAL}s buffer_size=${BUFFER_SIZE}"
    fi
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
# Safe to call every boot — every change uses an IF NOT EXISTS or checks
# PRAGMA table_info first.
###############################################################################
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

function migrate_database() {
    log_debug "Checking database schema at $DB_FILE"

    # If the database doesn't exist yet there is nothing to migrate;
    # initialize_database() runs immediately after this and creates it fresh
    # with the new schema.
    [ -f "$DB_FILE" ] || return 0

    # Enable WAL once. WAL persists as a database attribute, so setting it
    # here is equivalent to setting it at database creation.
    sqlite3 -init /dev/null "$DB_FILE" "PRAGMA journal_mode=WAL;" >/dev/null 2>&1

    # Add gpu_index if missing
    if ! gpu_metrics_has_column gpu_index; then
        log_warning "Migrating gpu_metrics: adding gpu_index column (default 0)"
        sqlite3 -init /dev/null "$DB_FILE" \
            "ALTER TABLE gpu_metrics ADD COLUMN gpu_index INTEGER NOT NULL DEFAULT 0;" || {
            log_error "Failed to add gpu_index column"
            return 1
        }
    fi

    # Add gpu_uuid if missing. Backfill existing rows to the current GPU's
    # UUID (the fork was single-GPU until now, so this is correct attribution).
    if ! gpu_metrics_has_column gpu_uuid; then
        local current_uuid escaped_uuid
        current_uuid=$(nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | head -n1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [ -z "$current_uuid" ] && current_uuid="legacy-unknown"

        # SQL-escape single quotes (external command output — UUIDs are
        # well-formed in practice but the escape keeps the pattern safe
        # and robust to future driver changes).
        escaped_uuid=${current_uuid//\'/\'\'}

        log_warning "Migrating gpu_metrics: adding gpu_uuid column (backfilling to '$current_uuid')"
        sqlite3 -init /dev/null "$DB_FILE" <<SQL
ALTER TABLE gpu_metrics ADD COLUMN gpu_uuid TEXT;
UPDATE gpu_metrics SET gpu_uuid = '$escaped_uuid' WHERE gpu_uuid IS NULL;
SQL
        if [ $? -ne 0 ]; then
            log_error "Failed to add gpu_uuid column"
            return 1
        fi
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
    sqlite3 "$DB_FILE" << EOF
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
# process_historical_data: Manages historical GPU metrics
# Handles data persistence and file permissions across system updates
# Creates JSON view for backward compatibility
###############################################################################
function process_historical_data() {
    local output_file="$HISTORY_DIR/history.json"
    
    # Create Python script for generating the JSON file from SQLite
    cat > /tmp/export_json.py << 'PYTHONSCRIPT'
import json
import sqlite3
import sys
import os
from datetime import datetime, timedelta

def export_history_json(db_path, output_path, retention_seconds):
    try:
        # Connect to SQLite database
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Cutoff comes from RETENTION_SECONDS passed in by the caller so there
        # is exactly one retention value in the whole codebase.
        cutoff_time = int(datetime.now().timestamp()) - retention_seconds
        
        # Query the database for everything newer than the computed cutoff
        cur = conn.cursor()
        cur.execute('''
            SELECT timestamp, temperature, utilization, memory, power
            FROM gpu_metrics
            WHERE timestamp_epoch > ?
            ORDER BY timestamp_epoch ASC
        ''', (cutoff_time,))
        
        # Prepare data structure
        result = {
            "timestamps": [],
            "temperatures": [],
            "utilizations": [],
            "memory": [],
            "power": []
        }
        
        # Process each row
        for row in cur.fetchall():
            result["timestamps"].append(row["timestamp"])
            result["temperatures"].append(row["temperature"])
            result["utilizations"].append(row["utilization"])
            result["memory"].append(row["memory"])
            result["power"].append(row["power"])
        
        # Create temp file first
        temp_path = output_path + ".tmp"
        with open(temp_path, 'w') as f:
            json.dump(result, f, indent=4)
        
        # Move temp file to final destination
        os.rename(temp_path, output_path)
        
        return True
    except Exception as e:
        print(f"Error exporting history to JSON: {e}", file=sys.stderr)
        return False
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <db_path> <output_json_path> <retention_seconds>", file=sys.stderr)
        sys.exit(1)

    try:
        retention_seconds = int(sys.argv[3])
    except ValueError:
        print(f"Error: retention_seconds must be an integer, got: {sys.argv[3]!r}", file=sys.stderr)
        sys.exit(2)

    success = export_history_json(sys.argv[1], sys.argv[2], retention_seconds)
    sys.exit(0 if success else 1)
PYTHONSCRIPT

    # Run the Python script to export data
    if ! python3 /tmp/export_json.py "$DB_FILE" "$output_file" "$RETENTION_SECONDS"; then
        log_error "Failed to export history data to JSON"
        return 1
    fi
    
    # Ensure proper permissions on the JSON file for web access
    chmod 666 "$output_file" 2>/dev/null
    
    return 0
}

# Function to process 24-hour stats
process_24hr_stats() {
    # Create Python script to generate stats from SQLite
    cat > /tmp/process_stats.py << 'EOF'
import sys
import json
import sqlite3
from datetime import datetime, timedelta

def get_24hr_stats(db_path):
    try:
        # Connect to SQLite database
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # Calculate cutoff time (24 hours ago)
        cutoff_time = int((datetime.now() - timedelta(hours=24)).timestamp())
        
        # Execute query to get min/max values
        cur = conn.cursor()
        cur.execute('''
            SELECT 
                MIN(temperature) as temp_min,
                MAX(temperature) as temp_max,
                MIN(utilization) as util_min,
                MAX(utilization) as util_max,
                MIN(memory) as mem_min,
                MAX(memory) as mem_max,
                MIN(CASE WHEN power > 0 THEN power ELSE NULL END) as power_min,
                MAX(power) as power_max
            FROM gpu_metrics
            WHERE timestamp_epoch > ?
        ''', (cutoff_time,))
        
        row = cur.fetchone()
        
        # Handle case where no data was processed
        if row['temp_min'] is None:
            temp_min = temp_max = util_min = util_max = mem_min = mem_max = power_min = power_max = 0
        else:
            temp_min = row['temp_min']
            temp_max = row['temp_max']
            util_min = row['util_min']
            util_max = row['util_max']
            mem_min = row['mem_min']
            mem_max = row['mem_max']
            power_min = row['power_min'] if row['power_min'] is not None else 0
            power_max = row['power_max'] if row['power_max'] is not None else 0
        
        # Create stats object
        stats = {
            "stats": {
                "temperature": {"min": temp_min, "max": temp_max},
                "utilization": {"min": util_min, "max": util_max},
                "memory": {"min": mem_min, "max": mem_max},
                "power": {"min": power_min, "max": power_max}
            }
        }
        
        return json.dumps(stats, indent=4)
    except Exception as e:
        print(f"Error processing 24hr stats: {e}", file=sys.stderr)
        return json.dumps({"stats": {
            "temperature": {"min": 0, "max": 0},
            "utilization": {"min": 0, "max": 0},
            "memory": {"min": 0, "max": 0},
            "power": {"min": 0, "max": 0}
        }})
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <db_path>", file=sys.stderr)
        sys.exit(1)
    
    print(get_24hr_stats(sys.argv[1]))
EOF

    # Run the Python script
    python3 /tmp/process_stats.py "$DB_FILE" > "$STATS_FILE"
    chmod 666 "$STATS_FILE"
    rm /tmp/process_stats.py
}

###############################################################################
# rotate_logs: Manages log file sizes and retention
# Rotates logs based on:
# - Size limit (5MB)
# - Age limit (25 hr)
# Handles: error.log, warning.log, gpu_stats.log
###############################################################################
rotate_logs() {
    local max_size=$((5 * 1024 * 1024))  # 5MB size limit
    local max_age=$((25 * 3600))      # 25hr retention
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

    # Retention comes from the single RETENTION_SECONDS constant at the top
    # of this file — keep in sync with export_history_json by design.
    local cutoff_time=$(( $(date +%s) - RETENTION_SECONDS ))

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
# Returns: 0 on success, 1 on failure
###############################################################################
function process_buffer() {
    local temp_file="${BUFFER_FILE}.tmp"
    local success=0
    
    # Create temp file with buffer contents
    if cp "$BUFFER_FILE" "$temp_file"; then
        # Clear original buffer only after successful copy
        > "$BUFFER_FILE"
        
        # Process buffer with Python and write to database
        cat > /tmp/process_buffer.py << 'PYTHONSCRIPT'
import os
import sys
import sqlite3
from datetime import datetime

def process_buffer(db_path, buffer_lines):
    # interval_s (the cadence the collector is currently running at) and
    # gpu_uuid (the sampled GPU) come from environment variables set by
    # the bash caller, not from the buffer lines themselves. This keeps
    # the buffer file format small and line-position stable across ticks.
    try:
        interval_s = int(os.environ.get('GPU_MONITOR_INTERVAL_S', '4'))
    except ValueError:
        interval_s = 4
    gpu_uuid = os.environ.get('GPU_MONITOR_GPU_UUID', 'legacy-unknown') or 'legacy-unknown'

    try:
        conn = sqlite3.connect(db_path)
        conn.execute('BEGIN TRANSACTION')

        # Phase 1 widens the INSERT to carry gpu_index, gpu_uuid, and
        # interval_s. Phase 2 makes gpu_index dynamic per-row; for now it
        # is pinned to 0 because the collector still only samples one GPU.
        stmt = '''
            INSERT INTO gpu_metrics
            (timestamp, timestamp_epoch, temperature, utilization, memory, power,
             gpu_index, gpu_uuid, interval_s)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        '''

        for line in buffer_lines:
            line = line.strip()
            if not line:
                continue

            parts = line.split(',')
            if len(parts) < 5:
                continue

            timestamp = parts[0]
            temperature = float(parts[1])
            utilization = float(parts[2])
            memory = float(parts[3])

            # Handle N/A power values
            try:
                power = float(parts[4]) if parts[4].strip() != 'N/A' else 0
            except (ValueError, AttributeError):
                power = 0

            # Parse timestamp to epoch. New format is "%Y-%m-%d %H:%M:%S";
            # fall back to the legacy yearless format for any stragglers in an
            # on-disk buffer from a pre-upgrade container.
            try:
                dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                current_year = datetime.now().year
                dt = datetime.strptime(f"{current_year} {timestamp}", "%Y %m-%d %H:%M:%S")
            timestamp_epoch = int(dt.timestamp())

            conn.execute(stmt,
                (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                 gpu_uuid, interval_s))
        
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
            # Also append to log file for backup
            cat "$temp_file" >> "$LOG_FILE"
        else
            log_error "Failed to process buffer into database"
        fi
        
        # Clean up
        rm -f /tmp/process_buffer.py
    else
        log_error "Failed to create temp buffer file"
    fi
    
    # Clean up temp file
    rm -f "$temp_file"
    
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
    local gpu_stats=$(nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,memory.used,power.draw \
                     --format=csv,noheader,nounits 2>/dev/null)
    
    if [[ -n "$gpu_stats" ]]; then
        # Verify write access before proceeding
        if ! touch "$BUFFER_FILE" 2>/dev/null; then
            log_error "Cannot write to buffer file"
            return 1
        fi

        # Buffer write with error handling
        if ! echo "$timestamp,$gpu_stats" >> "$BUFFER_FILE"; then
            log_error "Failed to write to buffer"
            write_failed=1
        fi

        # Detailed error logging for debugging
        if [[ $write_failed -eq 1 ]]; then
            log_error "Buffer write details:"
            ls -l "$BUFFER_FILE" 2>&1 | log_error
            df -h "$(dirname "$BUFFER_FILE")" 2>&1 | log_error
        fi

        # Update current stats JSON for real-time display
        local temp=$(echo "$gpu_stats" | cut -d',' -f1 | tr -d ' ')
        local util=$(echo "$gpu_stats" | cut -d',' -f2 | tr -d ' ')
        local mem=$(echo "$gpu_stats" | cut -d',' -f3 | tr -d ' ')
        local power=$(echo "$gpu_stats" | cut -d',' -f4 | tr -d ' []')

        # Handle N/A power value
        if [[ "$power" == "N/A" || -z "$power" || "$power" == "[N/A]" ]]; then
            power="0"
        fi
        
        # Create JSON content
        local json_content=$(cat << EOF
{
    "timestamp": "$timestamp",
    "temperature": $temp,
    "utilization": $util,
    "memory": $mem,
    "power": $power
}
EOF
)
        # Write JSON safely
        safe_write_json "$JSON_FILE" "$json_content"

        # Process buffer when full
        if [[ -f "$BUFFER_FILE" ]] && [[ $(wc -l < "$BUFFER_FILE") -ge $BUFFER_SIZE ]]; then
            process_buffer
            process_historical_data
            process_24hr_stats
        fi
    else
        log_error "Failed to get GPU stats output"
    fi
}

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