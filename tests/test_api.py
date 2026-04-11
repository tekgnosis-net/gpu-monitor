"""
Integration tests for the Phase 3 aiohttp API.

These tests spin up the server against a fixture SQLite database seeded
with known multi-GPU rows and assert that each /api/* endpoint returns
the expected shape. Uses aiohttp.test_utils.TestServer so no actual
container or filesystem setup is required.

Run with:
    pytest tests/test_api.py -q

Requires: pytest, pytest-asyncio (aiohttp>=3.9 includes test helpers
but pytest-asyncio or pytest-aiohttp is the idiomatic runner).
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


# Make the collector's src/ directory importable so we can `import server`.
# The file lives at src/server.py relative to the repo root; this module
# lives at tests/test_api.py, so the repo root is one directory up.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
import server as server_module  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    """Create a temp BASE_DIR with a seeded SQLite DB, gpu_inventory.json,
    and VERSION file — then monkeypatch the server module's paths to
    point at it. Each test gets a fresh directory so state cannot bleed
    between tests."""
    base = tmp_path / "app"
    base.mkdir()
    history_dir = base / "history"
    history_dir.mkdir()

    # VERSION file
    (base / "VERSION").write_text("1.0.0-test\n")

    # gpu_inventory.json — 2 synthetic GPUs
    inventory = {
        "gpus": [
            {
                "index": 0,
                "uuid": "GPU-00000000-0000-0000-0000-000000000000",
                "name": "Test Card A",
                "memory_total_mib": 24576,
                "power_limit_w": 450,
            },
            {
                "index": 1,
                "uuid": "GPU-11111111-1111-1111-1111-111111111111",
                "name": "Test Card B",
                "memory_total_mib": 16384,
                "power_limit_w": 320,
            },
        ],
    }
    (base / "gpu_inventory.json").write_text(json.dumps(inventory))

    # Seed the SQLite DB with a handful of rows per GPU across the last
    # few minutes, so every API endpoint has data to return.
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

    # 5 rows per GPU, 10 seconds apart, ending at "now".
    now = int(datetime.now(timezone.utc).timestamp())
    gpu_rows = [
        # (gpu_index, gpu_uuid, base_temp, base_util, base_mem, base_power)
        (0, "GPU-00000000-0000-0000-0000-000000000000", 55.0, 42.0, 8192.0, 280.0),
        (1, "GPU-11111111-1111-1111-1111-111111111111", 48.0, 18.0, 4096.0, 150.0),
    ]
    for gi, gu, t, u, m, p in gpu_rows:
        for i in range(5):
            ts_epoch = now - (4 - i) * 10  # oldest first
            ts_str = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                INSERT INTO gpu_metrics
                (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                 gpu_index, gpu_uuid, interval_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 4)
                """,
                (ts_str, ts_epoch, t + i, u + i, m, p + i, gi, gu),
            )
    conn.commit()
    conn.close()

    # Monkey-patch the server module's path constants
    monkeypatch.setattr(server_module, "BASE_DIR", base)
    monkeypatch.setattr(server_module, "VERSION_FILE", base / "VERSION")
    monkeypatch.setattr(server_module, "INVENTORY_FILE", base / "gpu_inventory.json")
    monkeypatch.setattr(server_module, "DB_FILE", db_path)

    return base


@pytest_asyncio.fixture
async def client(tmp_base):
    """Spin up the aiohttp TestServer against the patched paths and
    yield a TestClient for the test body. Uses @pytest_asyncio.fixture
    rather than @pytest.fixture — pytest-asyncio in strict mode refuses
    to let a sync-looking fixture decorator wrap an async generator."""
    app = server_module.make_app()
    async with TestServer(app) as ts:
        async with TestClient(ts) as c:
            yield c


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health(client):
    """/api/health returns ok + version + schema."""
    resp = await client.get("/api/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["ok"] is True
    assert data["version"] == "1.0.0-test"
    assert data["schema"] == 2


@pytest.mark.asyncio
async def test_version(client):
    """/api/version returns just the version string."""
    resp = await client.get("/api/version")
    assert resp.status == 200
    data = await resp.json()
    assert data == {"version": "1.0.0-test"}


@pytest.mark.asyncio
async def test_gpus(client):
    """/api/gpus returns the inventory array."""
    resp = await client.get("/api/gpus")
    assert resp.status == 200
    data = await resp.json()
    assert "gpus" in data
    assert len(data["gpus"]) == 2
    assert data["gpus"][0]["index"] == 0
    assert data["gpus"][0]["name"] == "Test Card A"
    assert data["gpus"][1]["memory_total_mib"] == 16384


@pytest.mark.asyncio
async def test_metrics_current(client):
    """/api/metrics/current returns the latest sample per GPU."""
    resp = await client.get("/api/metrics/current")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    # Rows are ordered by gpu_index ASC. The fixture's last row per GPU
    # is temp + 4 (i=4 is oldest, so the highest index is newest... wait,
    # fixture logic: oldest at i=0, newest at i=4). Newest row for GPU 0
    # has temp = 55+4 = 59, GPU 1 has temp = 48+4 = 52.
    g0 = data[0]
    g1 = data[1]
    assert g0["gpu_index"] == 0
    assert g0["gpu_uuid"] == "GPU-00000000-0000-0000-0000-000000000000"
    assert g0["temperature"] == 59.0
    assert g1["gpu_index"] == 1
    assert g1["gpu_uuid"] == "GPU-11111111-1111-1111-1111-111111111111"
    assert g1["temperature"] == 52.0


@pytest.mark.asyncio
async def test_metrics_history_default_range(client):
    """/api/metrics/history with no range returns 24h window."""
    resp = await client.get("/api/metrics/history?gpu=0")
    assert resp.status == 200
    data = await resp.json()
    assert set(data.keys()) == {
        "timestamps", "temperatures", "utilizations", "memory", "power",
    }
    # Fixture has 5 rows per GPU, all within the last minute → all 5
    # should be in the 24h window.
    assert len(data["timestamps"]) == 5
    assert len(data["temperatures"]) == 5
    # Oldest row's temp is 55, newest is 59
    assert data["temperatures"][0] == 55.0
    assert data["temperatures"][-1] == 59.0


@pytest.mark.asyncio
async def test_metrics_history_gpu_filter(client):
    """/api/metrics/history?gpu=1 returns only GPU 1's rows."""
    resp = await client.get("/api/metrics/history?range=24h&gpu=1")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["temperatures"]) == 5
    # GPU 1 temps are 48..52, not GPU 0's 55..59
    assert data["temperatures"][0] == 48.0
    assert data["temperatures"][-1] == 52.0


@pytest.mark.asyncio
async def test_metrics_history_invalid_range_falls_back(client):
    """Unknown range values fall back to the 24h default, not 400."""
    resp = await client.get("/api/metrics/history?range=eternity&gpu=0")
    assert resp.status == 200
    data = await resp.json()
    assert len(data["timestamps"]) == 5


@pytest.mark.asyncio
async def test_stats_24h(client):
    """/api/stats/24h returns per-GPU min/max wrapped in a stats object."""
    resp = await client.get("/api/stats/24h")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)
    assert len(data) == 2

    g0 = data[0]
    assert g0["gpu_index"] == 0
    stats = g0["stats"]
    assert stats["temperature"]["min"] == 55.0
    assert stats["temperature"]["max"] == 59.0
    assert stats["utilization"]["min"] == 42.0
    assert stats["utilization"]["max"] == 46.0
    # memory is constant at 8192 for GPU 0
    assert stats["memory"]["min"] == 8192.0
    assert stats["memory"]["max"] == 8192.0


@pytest.mark.asyncio
async def test_static_catchall_404(client):
    """Requests that don't hit /api/* and don't match a static file
    return 404, not 500."""
    resp = await client.get("/does-not-exist.txt")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_static_path_traversal_is_rejected(client):
    """Path traversal attempts that escape BASE_DIR are rejected with 403."""
    resp = await client.get("/../../../etc/passwd")
    # aiohttp router normalizes `..` segments before dispatch, so this
    # usually ends up as /etc/passwd which our handler resolves relative
    # to BASE_DIR and returns 404 (file not found inside BASE_DIR).
    # Either 403 or 404 is acceptable; we just don't want a 500 or the
    # actual contents of /etc/passwd.
    assert resp.status in (403, 404)
