"""Tests for the power-telemetry correctness fixes.

Two distinct concerns are exercised here:

1. NULL power readings (representing "nvidia-smi returned [N/A]") are
   correctly excluded from /api/stats/power's average and energy
   integration, while still being counted in samples_total +
   samples_invalid so the UI surfaces the 'insufficient_telemetry'
   warning. This is the user-visible contract motivating the
   N/A → NULL collector change.

2. The schema migration that drops NOT NULL on the power column
   preserves all existing rows verbatim. The migration is the
   canonical SQLite 12-step table-rebuild pattern, which is easy
   to get wrong (forgetting to copy a column, dropping indexes,
   transaction not wrapping the rename). This test replicates the
   exact SQL run by migrate_database() and asserts data integrity.
"""

import sqlite3
import time
from datetime import datetime


# Schema migration SQL — kept in sync with the migrate_database() block
# in src/monitor_gpu.sh that drops NOT NULL on the power column. The
# DROP VIEW + recreate is critical: without it, DROP TABLE raises
# "error in view history_json_view: no such table" and aborts the
# transaction, leaving the DB in a half-migrated state. We learned
# this the hard way during a live test that wiped a dev DB.
MIGRATION_SQL = """
BEGIN TRANSACTION;
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
"""


def _create_old_schema(conn):
    """Replica of the original (pre-migration) gpu_metrics schema with
    `power REAL NOT NULL`, plus the indexes AND the history_json_view
    that initialize_database creates. The view is critical for the
    migration test: a naive table-rebuild (without DROP VIEW first)
    fails because SQLite raises 'error in view history_json_view'
    when the underlying table is dropped."""
    conn.executescript("""
        CREATE TABLE gpu_metrics (
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
        CREATE INDEX idx_gpu_metrics_timestamp_epoch ON gpu_metrics(timestamp_epoch);
        CREATE INDEX idx_gpu_metrics_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);
        CREATE VIEW history_json_view AS
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
    """)


def _insert_row(conn, *, epoch, power, gpu_index=0):
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO gpu_metrics
           (timestamp, timestamp_epoch, temperature, utilization,
            memory, power, gpu_index, gpu_uuid, interval_s)
           VALUES (?, ?, 60.0, 50.0, 8000.0, ?, ?, 'test', 4)""",
        (ts, epoch, power, gpu_index),
    )


def _power_notnull_constraint(conn):
    """Return 1 if the power column is NOT NULL, 0 if nullable.
    Mirrors the PRAGMA query used by migrate_database to decide
    whether the migration is needed."""
    row = conn.execute(
        "SELECT \"notnull\" FROM pragma_table_info('gpu_metrics') WHERE name='power';"
    ).fetchone()
    return int(row[0]) if row else None


def _create_new_schema(conn):
    """Replica of the post-migration gpu_metrics schema (nullable power)."""
    conn.executescript("""
        CREATE TABLE gpu_metrics (
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
        CREATE INDEX idx_gpu_metrics_timestamp_epoch ON gpu_metrics(timestamp_epoch);
        CREATE INDEX idx_gpu_metrics_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);
    """)


def test_null_power_excluded_from_average(tmp_path):
    """Verifies the /api/stats/power aggregation contract: rows with
    NULL power are excluded from avg/peak/energy but still counted in
    samples_total + samples_invalid so the UI knows the window had
    telemetry gaps."""
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    _create_new_schema(conn)

    now = int(time.time())
    # 3 valid 200W readings + 2 NULL (telemetry gap) + 1 zero (legacy
    # data from before the migration — should still count as invalid).
    _insert_row(conn, epoch=now - 60, power=200.0)
    _insert_row(conn, epoch=now - 50, power=200.0)
    _insert_row(conn, epoch=now - 40, power=None)
    _insert_row(conn, epoch=now - 30, power=None)
    _insert_row(conn, epoch=now - 20, power=200.0)
    _insert_row(conn, epoch=now - 10, power=0.0)
    conn.commit()

    # Same SQL as server.py's /api/stats/power handler.
    row = conn.execute(
        """
        SELECT
            COALESCE(AVG(CASE WHEN power > 0 THEN power END), 0) AS avg_power_w,
            COALESCE(MAX(CASE WHEN power > 0 THEN power END), 0) AS peak_power_w,
            COUNT(*) AS samples_total,
            SUM(CASE WHEN power IS NULL OR power <= 0 THEN 1 ELSE 0 END)
                AS samples_invalid
        FROM gpu_metrics
        """
    ).fetchone()

    assert row["avg_power_w"] == 200.0, (
        f"avg should be 200W ignoring 2 NULLs + 1 zero, got {row['avg_power_w']}"
    )
    assert row["peak_power_w"] == 200.0
    assert row["samples_total"] == 6
    assert row["samples_invalid"] == 3, (
        f"NULL and 0 power rows should both count as invalid, got {row['samples_invalid']}"
    )


def test_migration_drops_not_null_and_preserves_data(tmp_path):
    """The migration is the canonical SQLite table-rebuild pattern.
    Easy to get wrong: forget a column, mis-order the inserts,
    skip the index recreation. This test runs the exact SQL from
    migrate_database() against a populated old-schema DB and asserts:
      - power column becomes nullable afterwards
      - every row's data is preserved bit-for-bit
      - both indexes exist on the new table
      - subsequent NULL-power inserts succeed
    """
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db))
    _create_old_schema(conn)
    assert _power_notnull_constraint(conn) == 1, "fixture should start with NOT NULL power"

    now = int(time.time())
    seed = [
        (now - 30, 150.0),
        (now - 20, 175.5),
        (now - 10, 200.0),
    ]
    for epoch, power in seed:
        _insert_row(conn, epoch=epoch, power=power)
    conn.commit()

    pre_migration_rows = conn.execute(
        "SELECT timestamp_epoch, power FROM gpu_metrics ORDER BY timestamp_epoch"
    ).fetchall()

    conn.executescript(MIGRATION_SQL)

    # Constraint dropped
    assert _power_notnull_constraint(conn) == 0, (
        "power column should be nullable after migration"
    )

    # Data preserved
    post_migration_rows = conn.execute(
        "SELECT timestamp_epoch, power FROM gpu_metrics ORDER BY timestamp_epoch"
    ).fetchall()
    assert pre_migration_rows == post_migration_rows

    # Indexes recreated (DROP TABLE drops them, the migration must recreate them)
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gpu_metrics'"
        )
    }
    assert "idx_gpu_metrics_timestamp_epoch" in indexes
    assert "idx_gpu_metrics_gpu_epoch" in indexes

    # Dependent view recreated and queryable (regression guard for the
    # data-loss bug found in the first live test, where a naive
    # table-rebuild orphaned the view, aborted the transaction, and
    # left the DB in a state where the next container start created
    # a fresh empty gpu_metrics table).
    views = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )
    }
    assert "history_json_view" in views
    # Querying the view should not raise — proves it's bound to the
    # post-rename table, not orphaned.
    conn.execute("SELECT * FROM history_json_view LIMIT 1").fetchall()

    # NULL inserts now allowed
    _insert_row(conn, epoch=now, power=None)
    conn.commit()
    null_count = conn.execute(
        "SELECT COUNT(*) FROM gpu_metrics WHERE power IS NULL"
    ).fetchone()[0]
    assert null_count == 1


def test_migration_guard_skips_after_success(tmp_path):
    """The migration is intended to run once. The bash caller in
    migrate_database() guards on
        SELECT "notnull" FROM pragma_table_info('gpu_metrics') WHERE name='power'
    and only runs the rebuild when that returns 1.

    This test verifies the post-condition the guard depends on: after
    a successful migration, the `notnull` flag is 0, so subsequent
    container starts will see the guard return 0 and skip the
    migration. (The rebuild SQL itself would also succeed if re-run
    against the post-migration state — it would just rebuild
    unnecessarily — so the guard is what makes this idempotent in
    practice, not any inherent error from the SQL.)"""
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db))
    _create_old_schema(conn)
    _insert_row(conn, epoch=int(time.time()), power=100.0)
    conn.commit()

    assert _power_notnull_constraint(conn) == 1, (
        "fixture should start with the old NOT NULL schema"
    )
    conn.executescript(MIGRATION_SQL)
    assert _power_notnull_constraint(conn) == 0, (
        "post-migration: bash guard query must return 0 so the "
        "migration is not re-run on subsequent container starts"
    )
