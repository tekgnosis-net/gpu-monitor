"""SQLite persistence layer for the GPU collector.

Replaces every interaction with the `sqlite3` CLI in the legacy bash
collector and the embedded `process_buffer.py` heredoc. All migrations,
schema initialization, and per-tick INSERTs go through this module.

Design notes
------------

* `migrate(db_path, current_uuid)` is idempotent and runs on every
  startup. It mirrors the legacy `migrate_database()` bash function:
  add `gpu_index`, `gpu_uuid`, and `interval_s` columns if missing;
  backfill NULL `gpu_uuid` rows with the current GPU's UUID; drop
  `NOT NULL` on `power` for pre-v1.5.0 schemas via the canonical
  SQLite 12-step table-rebuild pattern (with `history_json_view`
  dropped + recreated inside the rebuild's transaction so SQLite
  doesn't raise "error in view: no such table" on the DROP).

* `initialize(db_path)` creates the fresh schema for a brand-new DB
  via `CREATE TABLE/INDEX/VIEW IF NOT EXISTS`. Idempotent. Always runs
  immediately after `migrate()`; the IF NOT EXISTS guards make this
  a no-op for already-migrated DBs.

* `insert_samples(db_path, samples, interval_s)` does a single bulk
  INSERT per tick. SQLite WAL mode means readers (server.py routes)
  see consistent snapshots without contending on the writer. Failures
  are raised; the caller (collector.run) catches them, logs a warning
  with sample contents, and continues to the next tick — no batched
  retry ladder, because direct per-tick writes mean a fail is one
  sample, not 30.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from gpu_monitor.state import GPUMetric

log = logging.getLogger("gpu-monitor.db")


# Indexes created on the gpu_metrics table.
INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_gpu_metrics_timestamp_epoch", "gpu_metrics(timestamp_epoch)"),
    ("idx_gpu_metrics_gpu_epoch", "gpu_metrics(gpu_index, timestamp_epoch)"),
)

# Fresh table schema — used by initialize() and as the rebuild target
# in _migrate_power_nullable(). The `power` column is nullable: missing
# telemetry (NVML_ERROR_NOT_SUPPORTED) is stored as SQL NULL so it can
# be cleanly excluded from aggregation queries via `power > 0` /
# `power IS NULL OR power <= 0`.
GPU_METRICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS gpu_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    timestamp_epoch INTEGER NOT NULL,
    temperature REAL NOT NULL,
    utilization REAL NOT NULL,
    memory REAL NOT NULL,
    power REAL,
    gpu_index INTEGER NOT NULL DEFAULT 0,
    gpu_uuid TEXT,
    interval_s INTEGER NOT NULL DEFAULT 4
);
"""

# View that the legacy frontend (and /api/metrics/history) reads.
# Aggregates the last 24h into a single JSON document.
HISTORY_VIEW_SQL = """
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
"""


# ─── Connection management ─────────────────────────────────────────────────


@contextmanager
def _connect(db_path: str | Path):
    """Open a SQLite connection, commit on clean exit, rollback on
    exception, always close. Mirrors the bash sqlite3 CLI's behavior
    where each invocation was its own atomic unit."""
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Enable WAL journal mode. Warn (not fail) if filesystem doesn't
    support it — losing concurrent-reader scalability is a perf
    degradation, not a correctness issue. Failing hard would be
    user-hostile for ephemeral dev deployments on tmpfs."""
    cur = conn.execute("PRAGMA journal_mode=WAL;")
    mode = cur.fetchone()[0]
    if mode != "wal":
        log.warning(
            "WAL not supported on this filesystem; SQLite reported %r. "
            "Concurrent-reader scalability will be reduced but correctness "
            "is unaffected.",
            mode,
        )


# ─── Schema introspection helpers ──────────────────────────────────────────


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column
        for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _column_notnull(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if the column has a NOT NULL constraint, False if
    nullable. Raises KeyError if the column doesn't exist."""
    for row in conn.execute(f"PRAGMA table_info({table})"):
        if row[1] == column:
            return bool(row[3])
    raise KeyError(f"column {column!r} not in table {table!r}")


# ─── Public API ────────────────────────────────────────────────────────────


def initialize(db_path: str | Path) -> None:
    """Create the gpu_metrics table, indexes, and history_json_view.

    Idempotent via IF NOT EXISTS — no-op on already-initialized DBs.
    Always called immediately after migrate(), so fresh installs get
    the v2.0.0 schema directly while existing installs get the
    migrated schema.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        _enable_wal(conn)
        conn.executescript(GPU_METRICS_SCHEMA)
        for name, target in INDEXES:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target};")
        conn.executescript(HISTORY_VIEW_SQL)


def migrate(db_path: str | Path, current_uuid: str = "legacy-unknown") -> None:
    """Idempotent schema migration for pre-v2.0.0 databases.

    Mirrors the legacy bash `migrate_database()` function. Safe to run
    on any DB at any version — no-op on already-current schemas.
    `current_uuid` is used to backfill rows inserted with NULL
    gpu_uuid (a partial-failure recovery case from earlier phases).

    Order of operations matters: each ALTER is its own transaction so
    a partial failure (e.g., disk full mid-rebuild) leaves the DB in a
    state where the next boot's migrate() can resume.
    """
    if not Path(db_path).exists():
        # Fresh install — initialize() handles it.
        return

    # Step 1: column additions and backfills. These are individually
    # idempotent and re-runnable.
    with _connect(db_path) as conn:
        if not _table_exists(conn, "gpu_metrics"):
            log.debug(
                "migrate: gpu_metrics table absent; skipping migration "
                "(initialize() will create the fresh schema)"
            )
            return

        _enable_wal(conn)

        if not _column_exists(conn, "gpu_metrics", "gpu_index"):
            log.warning("Migrating gpu_metrics: adding gpu_index column (default 0)")
            conn.execute(
                "ALTER TABLE gpu_metrics ADD COLUMN gpu_index "
                "INTEGER NOT NULL DEFAULT 0;"
            )

        if not _column_exists(conn, "gpu_metrics", "gpu_uuid"):
            log.warning("Migrating gpu_metrics: adding gpu_uuid column")
            conn.execute("ALTER TABLE gpu_metrics ADD COLUMN gpu_uuid TEXT;")

        # Backfill NULL gpu_uuid (idempotent; runs on every boot to
        # recover from partial previous failures or rows inserted
        # before the column was populated).
        null_count = conn.execute(
            "SELECT COUNT(*) FROM gpu_metrics WHERE gpu_uuid IS NULL"
        ).fetchone()[0]
        if null_count > 0:
            uuid = current_uuid or "legacy-unknown"
            log.warning(
                "Backfilling %d gpu_metrics row(s) with NULL gpu_uuid → %r",
                null_count, uuid,
            )
            conn.execute(
                "UPDATE gpu_metrics SET gpu_uuid = ? WHERE gpu_uuid IS NULL",
                (uuid,),
            )

        if not _column_exists(conn, "gpu_metrics", "interval_s"):
            log.warning(
                "Migrating gpu_metrics: adding interval_s column (default 4)"
            )
            conn.execute(
                "ALTER TABLE gpu_metrics ADD COLUMN interval_s "
                "INTEGER NOT NULL DEFAULT 4;"
            )

        # Composite index for (gpu_index, timestamp_epoch) queries
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gpu_metrics_gpu_epoch "
            "ON gpu_metrics(gpu_index, timestamp_epoch);"
        )

    # Step 2: drop NOT NULL on power column (pre-v1.5.0 → v1.5.0+).
    # This uses the SQLite 12-step rebuild pattern; runs in a separate
    # connection because executescript() issues an implicit COMMIT
    # which would prematurely close the column-add transaction above.
    with _connect(db_path) as conn:
        if _column_notnull(conn, "gpu_metrics", "power"):
            log.warning(
                "Migrating gpu_metrics: dropping NOT NULL on power column "
                "to allow N/A telemetry as SQL NULL"
            )
            _migrate_power_nullable(conn)


def _migrate_power_nullable(conn: sqlite3.Connection) -> None:
    """Drop NOT NULL on power via SQLite's canonical 12-step
    table-rebuild pattern.

    The dependent `history_json_view` is dropped before the table
    rebuild and recreated after — without this, `DROP TABLE
    gpu_metrics` raises "error in view history_json_view: no such
    table" and aborts the transaction, leaving the DB half-migrated.
    We learned this the hard way during a v1.5.0 live test that wiped
    a dev DB.

    Wrapped in BEGIN/COMMIT inside `executescript` so the migration is
    atomic. On exception, the outer `_connect()` context manager calls
    `rollback()` and the rebuild is fully reverted.
    """
    conn.executescript("""
BEGIN;
DROP VIEW IF EXISTS history_json_view;
CREATE TABLE gpu_metrics_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    timestamp_epoch INTEGER NOT NULL,
    temperature REAL NOT NULL,
    utilization REAL NOT NULL,
    memory REAL NOT NULL,
    power REAL,
    gpu_index INTEGER NOT NULL DEFAULT 0,
    gpu_uuid TEXT,
    interval_s INTEGER NOT NULL DEFAULT 4
);
INSERT INTO gpu_metrics_new
    (id, timestamp, timestamp_epoch, temperature, utilization, memory,
     power, gpu_index, gpu_uuid, interval_s)
    SELECT id, timestamp, timestamp_epoch, temperature, utilization, memory,
           power, gpu_index, gpu_uuid, interval_s
    FROM gpu_metrics;
DROP TABLE gpu_metrics;
ALTER TABLE gpu_metrics_new RENAME TO gpu_metrics;
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_timestamp_epoch ON gpu_metrics(timestamp_epoch);
CREATE INDEX IF NOT EXISTS idx_gpu_metrics_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);
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
COMMIT;
""")


def insert_samples(
    db_path: str | Path,
    samples: Iterable[GPUMetric],
    interval_s: int,
) -> int:
    """Bulk INSERT one tick's GPUMetric rows into gpu_metrics.

    Returns the number of rows inserted. Raises sqlite3.Error on
    failure — the caller logs and continues; we do not retry, since
    each tick is its own transaction and a single dropped sample is
    a strictly better failure mode than the legacy 30-sample-batch
    flush retry that motivated the bash `.pending` machinery.
    """
    rows = [
        (
            _format_timestamp(s.timestamp_epoch),
            s.timestamp_epoch,
            s.temperature,
            s.utilization,
            s.memory_mib,
            s.power_w,  # None → SQL NULL for missing power telemetry
            s.gpu_index,
            s.gpu_uuid,
            interval_s,
        )
        for s in samples
    ]
    if not rows:
        return 0
    with _connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO gpu_metrics
               (timestamp, timestamp_epoch, temperature, utilization, memory,
                power, gpu_index, gpu_uuid, interval_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


def _format_timestamp(epoch: int) -> str:
    """Render epoch as 'YYYY-MM-DD HH:MM:SS' in local time, matching
    the legacy bash collector's `date '+%Y-%m-%d %H:%M:%S'` output.
    The format is consumed by /api/metrics/history's JSON view."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
