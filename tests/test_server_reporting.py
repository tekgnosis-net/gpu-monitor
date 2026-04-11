"""
Integration tests for the Phase 6b server routes:
    POST /api/settings/smtp/test
    POST /api/schedules/{id}/run-now
    GET  /api/reports/preview

These exercise the full render → mailer → SMTP wire path through
the aiohttp TestServer using the same inline asyncio SMTP catcher
as test_mailer.py / test_scheduler.py.
"""

from __future__ import annotations

import asyncio
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
from reporting import crypto as crypto_module  # noqa: E402

# Reuse the inline SMTP listener from test_mailer
sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_mailer import _handle_smtp_client, SmtpCatcher  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def smtp_catcher():
    """Start an inline asyncio SMTP listener and yield (host, port, catcher)."""
    catcher = SmtpCatcher()
    server = await asyncio.start_server(
        lambda r, w: _handle_smtp_client(r, w, catcher),
        host="127.0.0.1",
        port=0,
    )
    sock = server.sockets[0]
    host, port = sock.getsockname()[:2]

    async def _serve():
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(_serve())
    try:
        yield (host, port, catcher)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server.close()
        await server.wait_closed()


@pytest.fixture
def tmp_base_full(tmp_path, monkeypatch):
    """Full /app stand-in with VERSION, gpu_inventory.json, seeded
    DB, and monkeypatched server module paths. No settings.json
    yet — tests write it themselves based on what they need."""
    base = tmp_path / "app"
    base.mkdir()
    history = base / "history"
    history.mkdir()

    (base / "VERSION").write_text("1.0.0-test\n")
    (base / "gpu_inventory.json").write_text(json.dumps({
        "gpus": [
            {"index": 0, "name": "Test Card", "uuid": "GPU-0",
             "memory_total_mib": 24576, "power_limit_w": 450},
        ],
    }))

    # Minimal DB with 30 rows so render has something to summarize
    db_path = history / "gpu_metrics.db"
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
    for i in range(30):
        ts_epoch = now - (29 - i) * 60
        ts_str = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO gpu_metrics
               (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                gpu_index, gpu_uuid, interval_s)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, 4)""",
            (ts_str, ts_epoch, 60.0, 50.0, 8192.0, 200.0, "GPU-0"),
        )
    conn.commit()
    conn.close()

    settings_path = base / "settings.json"
    secret_path = history / ".secret"

    monkeypatch.setattr(server_module, "BASE_DIR", base)
    monkeypatch.setattr(server_module, "VERSION_FILE", base / "VERSION")
    monkeypatch.setattr(server_module, "INVENTORY_FILE", base / "gpu_inventory.json")
    monkeypatch.setattr(server_module, "DB_FILE", db_path)
    monkeypatch.setattr(server_module, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(server_module, "SECRET_KEY_FILE", secret_path)
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)

    return base


@pytest_asyncio.fixture
async def client(tmp_base_full):
    app = server_module.make_app()
    async with TestServer(app) as ts:
        async with TestClient(ts) as c:
            yield c


def _write_settings_with_smtp(base: Path, host: str, port: int,
                               schedules: list[dict] | None = None) -> None:
    """Write a settings.json with SMTP pointed at the inline listener."""
    data = {
        "collection": {"interval_seconds": 4, "flush_interval_seconds": 60},
        "housekeeping": {"retention_days": 3},
        "logging": {"max_size_mb": 5, "max_age_hours": 25},
        "alerts": {
            "temperature_c": 80, "utilization_pct": 100, "power_w": 300,
            "cooldown_seconds": 10, "sound_enabled": True,
            "notifications_enabled": False,
        },
        "power": {"rate_per_kwh": 0.15, "currency": "$"},
        "smtp": {
            "host": host, "port": port, "user": "",
            "password_enc": "", "from": "server@test.local", "tls": "none",
        },
        "schedules": schedules or [],
        "theme": {"default_mode": "auto"},
    }
    (base / "settings.json").write_text(json.dumps(data, indent=2))


# ─── POST /api/settings/smtp/test ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_smtp_test_sends_real_message(client, tmp_base_full, smtp_catcher):
    """With SMTP configured, POST /api/settings/smtp/test actually
    sends a message and the inline listener captures it."""
    host, port, catcher = smtp_catcher
    _write_settings_with_smtp(tmp_base_full, host, port)

    resp = await client.post("/api/settings/smtp/test", json={
        "to": "recipient@test.local",
    })
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["ok"] is True
    assert data["to"] == "recipient@test.local"
    assert data["from"] == "server@test.local"

    # Message landed at the catcher
    assert len(catcher.messages) == 1
    captured = catcher.messages[0]
    assert captured["mail_from"] == "server@test.local"
    assert "recipient@test.local" in captured["rcpt_tos"]


@pytest.mark.asyncio
async def test_smtp_test_bad_host_returns_502(client, tmp_base_full):
    """SMTP host configured but pointing at an unreachable port
    returns 502 with the underlying error message."""
    _write_settings_with_smtp(tmp_base_full, "127.0.0.1", 1)  # port 1, refused

    resp = await client.post("/api/settings/smtp/test")
    assert resp.status == 502
    data = await resp.json()
    assert data["ok"] is False
    assert "connection failed" in data["error"].lower() or "send failed" in data["error"].lower()


@pytest.mark.asyncio
async def test_smtp_test_cross_origin_rejected(client, tmp_base_full, smtp_catcher):
    """Same-origin defense applies to smtp/test too."""
    host, port, _ = smtp_catcher
    _write_settings_with_smtp(tmp_base_full, host, port)

    resp = await client.post(
        "/api/settings/smtp/test",
        headers={"Origin": "https://evil.example.com", "Host": "gpu.local"},
    )
    assert resp.status == 403


# ─── POST /api/schedules/{id}/run-now ──────────────────────────────────────


@pytest.mark.asyncio
async def test_run_now_fires_schedule(client, tmp_base_full, smtp_catcher):
    """Schedule id resolution + render + send → ok + last_run_epoch
    persisted."""
    host, port, catcher = smtp_catcher
    _write_settings_with_smtp(tmp_base_full, host, port, schedules=[
        {
            "id": "daily-test", "template": "daily",
            "cron": "0 8 * * *",
            "recipients": ["ops@test.local"],
            "enabled": True,
            "last_run_epoch": None,
        },
    ])

    resp = await client.post("/api/schedules/daily-test/run-now")
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    assert data["ok"] is True
    assert data["schedule_id"] == "daily-test"
    assert "ops@test.local" in data["recipients"]
    assert data["last_run_epoch"] > 0

    # Message landed — with embedded charts this time (full render)
    assert len(catcher.messages) == 1
    captured = catcher.messages[0]
    assert "ops@test.local" in captured["rcpt_tos"]

    # last_run_epoch persisted on disk
    saved = json.loads((tmp_base_full / "settings.json").read_text())
    assert saved["schedules"][0]["last_run_epoch"] == data["last_run_epoch"]


@pytest.mark.asyncio
async def test_run_now_unknown_schedule_returns_404(client, tmp_base_full, smtp_catcher):
    """Nonexistent schedule id returns 404 instead of 500."""
    host, port, _ = smtp_catcher
    _write_settings_with_smtp(tmp_base_full, host, port, schedules=[])

    resp = await client.post("/api/schedules/ghost/run-now")
    assert resp.status == 404
    data = await resp.json()
    assert data["ok"] is False
    assert "not found" in data["error"]


@pytest.mark.asyncio
async def test_run_now_no_recipients_returns_400(client, tmp_base_full, smtp_catcher):
    """Schedule exists but has no recipients → 400, not 500."""
    host, port, _ = smtp_catcher
    _write_settings_with_smtp(tmp_base_full, host, port, schedules=[
        {
            "id": "broken", "template": "daily",
            "cron": "0 8 * * *", "recipients": [],
            "enabled": True, "last_run_epoch": None,
        },
    ])

    resp = await client.post("/api/schedules/broken/run-now")
    assert resp.status == 400
    data = await resp.json()
    assert "no recipients" in data["error"]


@pytest.mark.asyncio
async def test_run_now_empty_smtp_returns_400(client, tmp_base_full):
    """Schedule exists but SMTP host is empty → 400 with a clear
    pointer to the Settings view."""
    _write_settings_with_smtp(tmp_base_full, "", 587, schedules=[
        {
            "id": "orphan", "template": "daily",
            "cron": "0 8 * * *", "recipients": ["x@y.z"],
            "enabled": True, "last_run_epoch": None,
        },
    ])

    resp = await client.post("/api/schedules/orphan/run-now")
    assert resp.status == 400
    data = await resp.json()
    assert "not configured" in data["error"]


# ─── GET /api/reports/preview ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_returns_html_body(client, tmp_base_full):
    """GET /api/reports/preview returns the rendered HTML for an
    iframe srcdoc. No settings.json required, no SMTP needed."""
    resp = await client.get("/api/reports/preview?template=daily")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/html")

    body = await resp.text()
    # Sanity checks on the rendered HTML
    assert "<html" in body
    assert "GPU Monitor" in body
    assert "Test Card" in body  # GPU name from the inventory
    # No cid: references because include_charts=False
    assert "cid:" not in body


@pytest.mark.asyncio
async def test_preview_unknown_template_returns_400(client, tmp_base_full):
    """An unknown template string returns 400 HTML (so the iframe
    shows the error instead of a browser 5xx page)."""
    resp = await client.get("/api/reports/preview?template=annually")
    assert resp.status == 400
    body = await resp.text()
    assert "Preview failed" in body
    assert "unknown template" in body


@pytest.mark.asyncio
async def test_preview_defaults_to_daily(client, tmp_base_full):
    """No template parameter → daily."""
    resp = await client.get("/api/reports/preview")
    assert resp.status == 200
    body = await resp.text()
    assert "daily report" in body
