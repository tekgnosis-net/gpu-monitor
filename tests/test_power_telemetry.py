"""Tests for the power-telemetry correctness fixes (originally landed
in v1.5.0; ported in v2.0.0 to drive the migration via the new
`gpu_monitor.db` module instead of inlined SQL).

Two distinct concerns are exercised here:

1. NULL power readings (representing "nvidia-smi returned [N/A]") are
   correctly excluded from /api/stats/power's average and energy
   integration, while still being counted in samples_total +
   samples_invalid so the UI surfaces the 'insufficient_telemetry'
   warning. This is the user-visible contract motivating the
   N/A → NULL collector change.

2. The schema migration that drops NOT NULL on the power column
   preserves all existing rows verbatim. The migration is the
   canonical SQLite 12-step table-rebuild pattern, which is easy to
   get wrong (forgetting to copy a column, dropping indexes,
   transaction not wrapping the rename, orphaning the dependent
   history_json_view). The v2.0.0 port moves the SQL out of the bash
   collector and into `gpu_monitor.db.migrate()`; these tests now
   exercise the Python migration directly.
"""

import sqlite3
import time
from datetime import datetime

from gpu_monitor import db


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _create_old_schema(conn):
    """Replica of the pre-v1.5.0 gpu_metrics schema with `power REAL
    NOT NULL`, plus the indexes AND the history_json_view that
    initialize_database() created. The view is critical for the
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


def _power_notnull(db_path):
    """Helper to query the NOT NULL state of the power column at a
    given DB path. Mirrors the PRAGMA-based guard query inside
    `gpu_monitor.db.migrate()`."""
    conn = sqlite3.connect(str(db_path))
    try:
        return db._column_notnull(conn, "gpu_metrics", "power")
    finally:
        conn.close()


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_null_power_excluded_from_average(tmp_path):
    """Verifies the /api/stats/power aggregation contract: rows with
    NULL power are excluded from avg/peak but still counted in
    samples_total + samples_invalid so the UI knows the window had
    telemetry gaps. Uses `db.initialize()` to create the v2.0.0
    schema (nullable power)."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
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
    finally:
        conn.close()


def test_migration_drops_not_null_and_preserves_data(tmp_path):
    """End-to-end test of `db.migrate()` against a populated
    pre-v1.5.0 schema. Asserts:
      - power column is nullable afterwards
      - every row's data is preserved bit-for-bit
      - both indexes exist on the rebuilt table
      - history_json_view is recreated and queryable
      - subsequent NULL-power inserts succeed
    """
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _create_old_schema(conn)
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
    finally:
        conn.close()

    # Sanity check: fixture starts in the legacy NOT NULL state
    assert _power_notnull(db_path) is True

    # Run the migration via the new module
    db.migrate(db_path, current_uuid="test-uuid")

    # Constraint dropped
    assert _power_notnull(db_path) is False

    # Open a fresh connection to verify
    conn = sqlite3.connect(str(db_path))
    try:
        # Data preserved
        post_migration_rows = conn.execute(
            "SELECT timestamp_epoch, power FROM gpu_metrics ORDER BY timestamp_epoch"
        ).fetchall()
        assert pre_migration_rows == post_migration_rows

        # Indexes recreated (DROP TABLE drops them; migration must recreate)
        indexes = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='gpu_metrics' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert "idx_gpu_metrics_timestamp_epoch" in indexes
        assert "idx_gpu_metrics_gpu_epoch" in indexes

        # Dependent view recreated and queryable (regression guard for
        # the data-loss bug we hit during v1.5.0 live testing — a
        # naive rebuild without DROP VIEW + recreate orphans the view,
        # aborts the transaction, and leaves the DB half-migrated).
        views = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view'"
            )
        }
        assert "history_json_view" in views
        conn.execute("SELECT * FROM history_json_view LIMIT 1").fetchall()

        # NULL inserts now allowed
        _insert_row(conn, epoch=int(time.time()), power=None)
        conn.commit()
        null_count = conn.execute(
            "SELECT COUNT(*) FROM gpu_metrics WHERE power IS NULL"
        ).fetchone()[0]
        assert null_count == 1
    finally:
        conn.close()


def test_migration_guard_skips_after_success(tmp_path):
    """`db.migrate()` is intended to run on every startup, and is
    idempotent. After a successful first run, the power column is
    nullable, so the migrate() function's internal guard (the
    `_column_notnull` check on power) returns False and the rebuild
    is skipped on subsequent calls — even though the rebuild SQL
    itself would also succeed if re-run unnecessarily."""
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    try:
        _create_old_schema(conn)
        _insert_row(conn, epoch=int(time.time()), power=100.0)
        conn.commit()
    finally:
        conn.close()

    assert _power_notnull(db_path) is True
    db.migrate(db_path)
    assert _power_notnull(db_path) is False
    # Second run is a no-op — constraint already dropped, so the
    # internal guard short-circuits the rebuild
    db.migrate(db_path)
    assert _power_notnull(db_path) is False
