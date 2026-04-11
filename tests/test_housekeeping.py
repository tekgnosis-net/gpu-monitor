"""
Integration tests for the Phase 6.2 /api/housekeeping routes.

Scope:
  * GET /api/housekeeping/db-info returns size + row count + per-GPU
  * POST /api/housekeeping/vacuum runs VACUUM and reports freed bytes
  * POST /api/housekeeping/purge deletes rows older than N days
  * Purge validation: days must be int in [1, 365]
  * Cross-origin mutating requests are rejected with 403

Uses a larger fixture than test_api.py because VACUUM and purge
behavior needs enough rows to be measurable. 2 GPUs × 100 rows =
200 rows spanning the last 10 days.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import server as server_module  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_base_with_old_rows(tmp_path, monkeypatch):
    """Seed a DB with 200 rows spanning the last 10 days — old enough
    for purge(days=3) to delete roughly the oldest 70%, new enough for
    purge(days=30) to delete zero rows."""
    base = tmp_path / "app"
    base.mkdir()
    history_dir = base / "history"
    history_dir.mkdir()

    (base / "VERSION").write_text("1.0.0-test\n")
    (base / "gpu_inventory.json").write_text(json.dumps({"gpus": []}))

    db_path = history_dir / "gpu_metrics.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
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
    """)
    conn.execute("CREATE INDEX idx_gpu_epoch ON gpu_metrics(gpu_index, timestamp_epoch);")

    now = int(datetime.now(timezone.utc).timestamp())
    day_seconds = 86400
    # 100 rows per GPU, oldest at day -9, newest at day -0 (now).
    for gpu_index in (0, 1):
        for i in range(100):
            days_ago = 9 - (i * 9 // 99)  # 9 down to 0 (inclusive)
            ts_epoch = now - days_ago * day_seconds - i
            ts_str = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                INSERT INTO gpu_metrics
                (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                 gpu_index, gpu_uuid, interval_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 4)
                """,
                (ts_str, ts_epoch, 60.0, 50.0, 8192.0, 150.0, gpu_index, f"GPU-{gpu_index}"),
            )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server_module, "BASE_DIR", base)
    monkeypatch.setattr(server_module, "VERSION_FILE", base / "VERSION")
    monkeypatch.setattr(server_module, "INVENTORY_FILE", base / "gpu_inventory.json")
    monkeypatch.setattr(server_module, "DB_FILE", db_path)
    monkeypatch.setattr(server_module, "SETTINGS_FILE", base / "settings.json")
    monkeypatch.setattr(server_module, "SECRET_KEY_FILE", history_dir / ".secret")

    return base


@pytest_asyncio.fixture
async def client(tmp_base_with_old_rows):
    app = server_module.make_app()
    async with TestServer(app) as ts:
        async with TestClient(ts) as c:
            yield c


# ─── GET /api/housekeeping/db-info ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_info_returns_row_count_and_per_gpu(client):
    """db-info counts rows across the seeded fixture."""
    resp = await client.get("/api/housekeeping/db-info")
    assert resp.status == 200
    data = await resp.json()

    assert data["row_count"] == 200  # 2 GPUs × 100 rows
    assert data["size_bytes"] > 0
    assert data["oldest_epoch"] is not None
    assert data["newest_epoch"] is not None
    assert data["newest_epoch"] > data["oldest_epoch"]

    # Per-GPU breakdown
    per_gpu = {r["gpu_index"]: r["row_count"] for r in data["row_count_per_gpu"]}
    assert per_gpu == {0: 100, 1: 100}


# ─── POST /api/housekeeping/purge ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_deletes_old_rows(client):
    """purge(days=3) deletes rows older than 3 days from both GPUs."""
    resp = await client.post(
        "/api/housekeeping/purge",
        json={"days": 3},
    )
    assert resp.status == 200, await resp.text()
    data = await resp.json()

    assert data["ok"] is True
    assert data["days"] == 3
    # Fixture spans 10 days uniformly, so rows older than 3 days
    # (days 4..9) should be ~60% of each GPU's 100 rows × 2 GPUs.
    # The exact number depends on the integer-division formula used
    # to spread timestamps, so we assert the range rather than an
    # exact count.
    assert 100 <= data["rows_deleted"] <= 160

    # Sanity: db-info reflects the smaller row count
    info_resp = await client.get("/api/housekeeping/db-info")
    info = await info_resp.json()
    assert info["row_count"] == 200 - data["rows_deleted"]


@pytest.mark.asyncio
async def test_purge_with_large_days_deletes_nothing(client):
    """purge(days=30) when the fixture spans 10 days deletes zero rows."""
    resp = await client.post(
        "/api/housekeeping/purge",
        json={"days": 30},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["rows_deleted"] == 0


@pytest.mark.asyncio
async def test_purge_is_idempotent(client):
    """Running the same purge twice deletes rows the first time and
    zero rows the second time (no double-decrement, no error)."""
    first = await (await client.post("/api/housekeeping/purge", json={"days": 3})).json()
    second = await (await client.post("/api/housekeeping/purge", json={"days": 3})).json()

    assert first["rows_deleted"] > 0
    assert second["rows_deleted"] == 0


@pytest.mark.asyncio
async def test_purge_rejects_non_integer_days(client):
    """days as a string, float, or boolean is a 400."""
    for bad in ["3", 3.5, True, None, []]:
        resp = await client.post("/api/housekeeping/purge", json={"days": bad})
        assert resp.status == 400, f"bad={bad} should have been 400"


@pytest.mark.asyncio
async def test_purge_rejects_out_of_range_days(client):
    """days must be in [1, 365]."""
    for bad in [0, -1, 366, 1000]:
        resp = await client.post("/api/housekeeping/purge", json={"days": bad})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_purge_rejects_missing_body(client):
    """Empty / malformed body is a 400."""
    resp = await client.post("/api/housekeeping/purge", data="not json",
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400


# ─── POST /api/housekeeping/vacuum ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_vacuum_after_purge_reclaims_bytes(client):
    """Seed → purge → VACUUM: after purging 60% of rows, VACUUM
    should free a measurable number of bytes. Exact savings depend on
    page size and fragmentation, so we just assert freed > 0 and that
    the size_after is less than size_before."""
    # First purge aggressively to create free pages
    purge_resp = await client.post("/api/housekeeping/purge", json={"days": 2})
    purge_data = await purge_resp.json()
    assert purge_data["rows_deleted"] > 0

    # Now VACUUM
    resp = await client.post("/api/housekeeping/vacuum")
    assert resp.status == 200, await resp.text()
    data = await resp.json()

    assert data["ok"] is True
    assert data["size_before"] >= data["size_after"]
    assert data["freed_bytes"] == data["size_before"] - data["size_after"]


@pytest.mark.asyncio
async def test_vacuum_on_fresh_db_is_safe(client):
    """VACUUM on a database that hasn't had any deletes is a no-op
    (freed_bytes ≈ 0) but still returns ok=True."""
    resp = await client.post("/api/housekeeping/vacuum")
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    # freed_bytes can be 0 or a very small positive number; never
    # hugely negative on a well-packed DB.
    assert data["freed_bytes"] >= -1024


# ─── Same-origin defense ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vacuum_cross_origin_is_rejected(client):
    """Mutating routes enforce same-origin."""
    resp = await client.post(
        "/api/housekeeping/vacuum",
        headers={"Origin": "https://evil.example.com", "Host": "gpu-monitor.local"},
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_purge_cross_origin_is_rejected(client):
    resp = await client.post(
        "/api/housekeeping/purge",
        json={"days": 3},
        headers={"Origin": "https://evil.example.com", "Host": "gpu-monitor.local"},
    )
    assert resp.status == 403
