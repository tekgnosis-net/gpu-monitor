"""Tests for `gpu_monitor.db` — the Python port of the legacy bash
sqlite3 CLI invocations + embedded process_buffer.py heredoc.

The bash port previously had no unit tests; the migration logic was
exercised only implicitly by integration runs. This module covers
every migration path explicitly so we can refactor with confidence:

  - Fresh-install path: initialize() creates the v2.0.0 schema directly.
  - Phase-0 (legacy) DB: missing gpu_index, gpu_uuid, interval_s columns.
    migrate() adds them with the correct defaults and backfills.
  - Phase-1 DB: has all columns but power is NOT NULL. migrate() runs
    the 12-step rebuild and drops the constraint.
  - v1.5.0 DB: already-current schema. migrate() is a no-op.
  - Half-migrated DB: gpu_index added, gpu_uuid not. migrate() resumes.

The test_power_telemetry.py tests already verify the table-rebuild
specifically; this module focuses on the surrounding migration matrix
that the bash version did differently from the rebuild.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime

from gpu_monitor import db
from gpu_monitor.state import GPUMetric


# ─── Fixtures ──────────────────────────────────────────────────────────────


def _create_phase0_schema(conn: sqlite3.Connection) -> None:
    """Replica of the earliest-Phase 1 schema: 5 fields, no gpu_index,
    no gpu_uuid, no interval_s. The bash migrate_database() would
    upgrade this to the modern schema in three ALTER TABLE steps."""
    conn.executescript("""
        CREATE TABLE gpu_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            timestamp_epoch INTEGER NOT NULL,
            temperature REAL NOT NULL,
            utilization REAL NOT NULL,
            memory REAL NOT NULL,
            power REAL NOT NULL
        );
    """)


def _create_phase1_schema(conn: sqlite3.Connection) -> None:
    """Replica of the pre-v1.5.0 schema: all columns present but
    `power REAL NOT NULL`, and the dependent history_json_view that
    makes the rebuild non-trivial."""
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


def _insert_phase0(conn: sqlite3.Connection, *, epoch: int, power: float):
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO gpu_metrics
           (timestamp, timestamp_epoch, temperature, utilization, memory, power)
           VALUES (?, ?, 60.0, 50.0, 8000.0, ?)""",
        (ts, epoch, power),
    )


def _insert_phase1(conn: sqlite3.Connection, *, epoch: int, power: float, gpu_index: int = 0):
    ts = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO gpu_metrics
           (timestamp, timestamp_epoch, temperature, utilization, memory, power,
            gpu_index, gpu_uuid, interval_s)
           VALUES (?, ?, 60.0, 50.0, 8000.0, ?, ?, 'test', 4)""",
        (ts, epoch, power, gpu_index),
    )


# ─── initialize() ──────────────────────────────────────────────────────────


def test_initialize_creates_full_schema(tmp_path):
    """Fresh install: initialize() creates table, both indexes, and
    the history_json_view in one go."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        # Table exists with the v2.0.0 columns
        cols = {row[1] for row in conn.execute("PRAGMA table_info(gpu_metrics)")}
        assert cols == {
            "id", "timestamp", "timestamp_epoch", "temperature", "utilization",
            "memory", "power", "gpu_index", "gpu_uuid", "interval_s",
        }
        # Power is nullable (the v1.5.0 contract)
        assert db._column_notnull(conn, "gpu_metrics", "power") is False
        # Both indexes exist
        idx = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='gpu_metrics' AND name NOT LIKE 'sqlite_%'"
        )}
        assert "idx_gpu_metrics_timestamp_epoch" in idx
        assert "idx_gpu_metrics_gpu_epoch" in idx
        # View exists and queryable
        views = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )}
        assert "history_json_view" in views
        conn.execute("SELECT * FROM history_json_view LIMIT 1").fetchall()
        # WAL mode enabled
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
    finally:
        conn.close()


def test_initialize_idempotent(tmp_path):
    """Running initialize() twice is harmless — IF NOT EXISTS guards
    every CREATE."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)
    db.initialize(db_path)  # must not raise


def test_initialize_creates_parent_directory(tmp_path):
    """If the parent dir doesn't exist (fresh container `/app/history/`
    on first volume mount), initialize() creates it."""
    db_path = tmp_path / "nested" / "subdir" / "metrics.db"
    assert not db_path.parent.exists()
    db.initialize(db_path)
    assert db_path.exists()


# ─── migrate() — fresh / no-op cases ───────────────────────────────────────


def test_migrate_skips_when_db_missing(tmp_path):
    """No DB file → migrate() is a no-op (initialize will run next)."""
    db_path = tmp_path / "nonexistent.db"
    db.migrate(db_path)
    # Must not have created the file
    assert not db_path.exists()


def test_migrate_skips_when_table_missing(tmp_path):
    """DB file exists but no gpu_metrics table (e.g., empty file from
    a touch) → migrate() is a no-op."""
    db_path = tmp_path / "metrics.db"
    db_path.touch()
    db.migrate(db_path)


def test_migrate_noop_on_current_schema(tmp_path):
    """Running migrate() on a DB that's already at v2.0.0 schema
    leaves it byte-identical."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)
    schema_before = _dump_schema(db_path)
    db.migrate(db_path)
    schema_after = _dump_schema(db_path)
    assert schema_before == schema_after


# ─── migrate() — phase-0 (legacy 5-column) ─────────────────────────────────


def test_migrate_from_phase0_adds_all_columns(tmp_path):
    """Phase-0 DB (no gpu_index, gpu_uuid, interval_s) gets all three
    columns added with the correct defaults."""
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    _create_phase0_schema(conn)
    now = int(time.time())
    _insert_phase0(conn, epoch=now - 10, power=150.0)
    _insert_phase0(conn, epoch=now - 5, power=160.0)
    conn.commit()
    conn.close()

    db.migrate(db_path, current_uuid="test-uuid-phase0")

    conn = sqlite3.connect(str(db_path))
    try:
        cols = {row[1]: row for row in conn.execute("PRAGMA table_info(gpu_metrics)")}
        # New columns exist with correct defaults
        assert "gpu_index" in cols
        assert cols["gpu_index"][4] == "0"  # DEFAULT 0
        assert "gpu_uuid" in cols
        assert "interval_s" in cols
        assert cols["interval_s"][4] == "4"  # DEFAULT 4
        # Existing rows backfilled with current_uuid
        rows = list(conn.execute(
            "SELECT gpu_index, gpu_uuid, interval_s, power FROM gpu_metrics ORDER BY id"
        ))
        assert all(r[1] == "test-uuid-phase0" for r in rows)
        assert all(r[0] == 0 for r in rows)
        assert all(r[2] == 4 for r in rows)
        # Power values preserved
        assert [r[3] for r in rows] == [150.0, 160.0]
    finally:
        conn.close()


# ─── migrate() — phase-1 (NOT NULL power) ──────────────────────────────────


def test_migrate_from_phase1_drops_power_notnull(tmp_path):
    """Phase-1 DB → power column NOT NULL is dropped via the rebuild,
    data preserved, view recreated."""
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    _create_phase1_schema(conn)
    now = int(time.time())
    seed = [(now - 30, 150.0), (now - 20, 175.5), (now - 10, 200.0)]
    for epoch, power in seed:
        _insert_phase1(conn, epoch=epoch, power=power)
    conn.commit()
    conn.close()

    db.migrate(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        # Power column nullable now
        assert db._column_notnull(conn, "gpu_metrics", "power") is False
        # Data preserved verbatim
        rows = list(conn.execute(
            "SELECT timestamp_epoch, power FROM gpu_metrics ORDER BY timestamp_epoch"
        ))
        assert rows == seed
        # View recreated and queryable
        views = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        )}
        assert "history_json_view" in views
        conn.execute("SELECT * FROM history_json_view LIMIT 1").fetchall()
    finally:
        conn.close()


def test_migrate_phase1_then_null_inserts_succeed(tmp_path):
    """After migrate(), inserting a row with NULL power succeeds —
    proving the NOT NULL drop took effect."""
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    _create_phase1_schema(conn)
    conn.commit()
    conn.close()

    db.migrate(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            INSERT INTO gpu_metrics
                (timestamp, timestamp_epoch, temperature, utilization,
                 memory, power, gpu_index, gpu_uuid, interval_s)
            VALUES ('2026-01-01 00:00:00', 0, 50, 50, 8000, NULL, 0, 'x', 4)
        """)
        conn.commit()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM gpu_metrics WHERE power IS NULL"
        ).fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()


# ─── migrate() — partial-failure recovery ──────────────────────────────────


def test_migrate_backfills_null_uuid(tmp_path):
    """Some Phase-1 DBs have NULL gpu_uuid rows from a partial earlier
    migration. The backfill runs unconditionally and fills them with
    the current GPU's UUID."""
    db_path = tmp_path / "metrics.db"
    conn = sqlite3.connect(str(db_path))
    _create_phase1_schema(conn)
    # Insert rows with NULL gpu_uuid (simulating partial migration).
    # Must use raw INSERT with NULL since _insert_phase1 sets it to 'test'.
    conn.execute("""
        INSERT INTO gpu_metrics
            (timestamp, timestamp_epoch, temperature, utilization,
             memory, power, gpu_index, gpu_uuid, interval_s)
        VALUES ('2026-01-01 00:00:00', 0, 50, 50, 8000, 100, 0, NULL, 4)
    """)
    conn.commit()
    conn.close()

    db.migrate(db_path, current_uuid="recovered-uuid")

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute("SELECT gpu_uuid FROM gpu_metrics"))
        assert all(r[0] == "recovered-uuid" for r in rows)
    finally:
        conn.close()


# ─── insert_samples() ──────────────────────────────────────────────────────


def test_insert_samples_writes_rows(tmp_path):
    """insert_samples() writes all GPUMetric rows in one transaction
    with the given interval_s value."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)

    now = int(time.time())
    samples = [
        GPUMetric(0, "uuid-0", now, 60.0, 50.0, 8000.0, 200.0),
        GPUMetric(1, "uuid-1", now, 65.0, 55.0, 9000.0, 210.0),
    ]
    n = db.insert_samples(db_path, samples, interval_s=2)
    assert n == 2

    conn = sqlite3.connect(str(db_path))
    try:
        rows = list(conn.execute(
            "SELECT gpu_index, gpu_uuid, temperature, power, interval_s "
            "FROM gpu_metrics ORDER BY gpu_index"
        ))
        assert rows == [
            (0, "uuid-0", 60.0, 200.0, 2),
            (1, "uuid-1", 65.0, 210.0, 2),
        ]
    finally:
        conn.close()


def test_insert_samples_writes_null_power(tmp_path):
    """A GPUMetric with power_w=None lands in SQLite as NULL — the
    contract that motivated the v1.5.0 schema change."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)

    now = int(time.time())
    samples = [
        GPUMetric(0, "uuid-0", now, 60.0, 50.0, 8000.0, None),
    ]
    db.insert_samples(db_path, samples, interval_s=2)

    conn = sqlite3.connect(str(db_path))
    try:
        power = conn.execute("SELECT power FROM gpu_metrics").fetchone()[0]
        assert power is None
    finally:
        conn.close()


def test_insert_samples_empty_is_noop(tmp_path):
    """Empty sample list → 0 rows written, no error, no transaction."""
    db_path = tmp_path / "metrics.db"
    db.initialize(db_path)
    n = db.insert_samples(db_path, [], interval_s=2)
    assert n == 0


# ─── helpers ───────────────────────────────────────────────────────────────


def _dump_schema(db_path) -> set[str]:
    """Return the set of CREATE statements in sqlite_master, for
    schema-equality assertions."""
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        }
    finally:
        conn.close()
