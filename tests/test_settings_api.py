"""
Integration tests for the Phase 6 /api/settings routes.

Scope:
  * GET /api/settings on a fresh container (no file) returns DEFAULT_SETTINGS
    with smtp.password_set=false and NO password_enc leaked
  * PUT /api/settings with a partial body merges into the current file
  * PUT with an invalid value (out of range) returns 400 with detail
  * PUT with invalid JSON returns 400
  * SMTP password transition sentinel:
      - password not in body          → existing password preserved
      - password = None               → existing preserved
      - password = ""                 → existing cleared
      - password = "newpass"          → encrypted and stored
  * GET after each PUT never returns the password ciphertext
  * Cross-origin PUT is rejected with 403
  * POST /api/settings/smtp/test returns 501 (Phase 6.1 stub)
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
from reporting import crypto as crypto_module  # noqa: E402


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_base(tmp_path, monkeypatch):
    """Minimal /app stand-in for the settings tests — no SQLite fixture
    needed because none of the /api/settings routes hit the DB."""
    base = tmp_path / "app"
    base.mkdir()
    history_dir = base / "history"
    history_dir.mkdir()
    (base / "VERSION").write_text("1.0.0-test\n")

    settings_path = base / "settings.json"
    secret_path = history_dir / ".secret"

    monkeypatch.setattr(server_module, "BASE_DIR", base)
    monkeypatch.setattr(server_module, "VERSION_FILE", base / "VERSION")
    monkeypatch.setattr(server_module, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(server_module, "SECRET_KEY_FILE", secret_path)

    # Make sure no stale GPU_MONITOR_SECRET from the host pollutes
    # key generation — each test gets a fresh key file under tmp_path.
    monkeypatch.delenv(crypto_module.ENV_VAR, raising=False)

    return base


@pytest_asyncio.fixture
async def client(tmp_base):
    app = server_module.make_app()
    async with TestServer(app) as ts:
        async with TestClient(ts) as c:
            yield c


# ─── GET /api/settings — first run ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_first_run_returns_defaults(client):
    """No settings.json yet → defaults + password_set=false, no password_enc."""
    resp = await client.get("/api/settings")
    assert resp.status == 200
    data = await resp.json()

    # Shape check — every top-level section present
    assert set(data.keys()) >= {
        "collection", "housekeeping", "logging", "alerts",
        "power", "smtp", "schedules", "theme",
    }
    # Defaults
    assert data["collection"]["interval_seconds"] == 4
    assert data["collection"]["flush_interval_seconds"] == 60
    assert data["housekeeping"]["retention_days"] == 3
    # SMTP redaction
    assert "password_enc" not in data["smtp"]
    assert data["smtp"]["password_set"] is False


# ─── PUT /api/settings ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_partial_merge_preserves_other_sections(client, tmp_base):
    """PUT {"power": {"rate_per_kwh": 0.18}} leaves collection/smtp/etc alone."""
    resp = await client.put(
        "/api/settings",
        json={"power": {"rate_per_kwh": 0.18}},
    )
    assert resp.status == 200, await resp.text()

    # Reload to confirm the merge landed on disk
    raw = json.loads((tmp_base / "settings.json").read_text())
    assert raw["power"]["rate_per_kwh"] == 0.18
    # Other sections unchanged
    assert raw["collection"]["interval_seconds"] == 4
    assert raw["smtp"]["password_enc"] == ""

    # GET should reflect the merged state
    resp2 = await client.get("/api/settings")
    data = await resp2.json()
    assert data["power"]["rate_per_kwh"] == 0.18


@pytest.mark.asyncio
async def test_put_invalid_interval_returns_400(client):
    """Pydantic rejects interval_seconds=1 (below the ge=2 bound)."""
    resp = await client.put(
        "/api/settings",
        json={"collection": {"interval_seconds": 1}},
    )
    assert resp.status == 400
    data = await resp.json()
    assert data["error"] == "validation failed"
    assert any("interval_seconds" in str(d) for d in data["detail"])


@pytest.mark.asyncio
async def test_put_invalid_json_returns_400(client):
    """Malformed JSON body returns 400, not 500."""
    resp = await client.put(
        "/api/settings",
        data="{ this is not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_put_body_not_object_returns_400(client):
    """A JSON array or scalar at the top level is rejected."""
    resp = await client.put("/api/settings", json=["not", "an", "object"])
    assert resp.status == 400


# ─── SMTP password transition sentinel ─────────────────────────────────────


@pytest.mark.asyncio
async def test_put_smtp_password_new_gets_encrypted(client, tmp_base):
    """smtp.password: 'newpass' is encrypted and stored in password_enc."""
    resp = await client.put(
        "/api/settings",
        json={"smtp": {"host": "smtp.example.com", "password": "newpass"}},
    )
    assert resp.status == 200, await resp.text()

    # The on-disk file should have a non-empty password_enc
    raw = json.loads((tmp_base / "settings.json").read_text())
    assert raw["smtp"]["password_enc"] != ""
    assert raw["smtp"]["password_enc"] != "newpass"  # not plaintext!

    # The response must NOT include password_enc
    data = await resp.json()
    assert "password_enc" not in data["smtp"]
    assert data["smtp"]["password_set"] is True


@pytest.mark.asyncio
async def test_put_smtp_password_absent_preserves_existing(client, tmp_base):
    """Updating smtp.host without sending password preserves the existing
    ciphertext — a common pattern for 'edit one field without re-typing
    the password'."""
    # First set a password
    await client.put(
        "/api/settings",
        json={"smtp": {"password": "original"}},
    )
    original_raw = json.loads((tmp_base / "settings.json").read_text())
    original_enc = original_raw["smtp"]["password_enc"]
    assert original_enc != ""

    # Now PUT without password field
    resp = await client.put(
        "/api/settings",
        json={"smtp": {"host": "new.example.com"}},
    )
    assert resp.status == 200

    updated_raw = json.loads((tmp_base / "settings.json").read_text())
    assert updated_raw["smtp"]["host"] == "new.example.com"
    assert updated_raw["smtp"]["password_enc"] == original_enc


@pytest.mark.asyncio
async def test_put_smtp_password_null_preserves_existing(client, tmp_base):
    """Explicit `password: null` in the body is the 'no change' sentinel."""
    await client.put("/api/settings", json={"smtp": {"password": "original"}})
    original_enc = json.loads((tmp_base / "settings.json").read_text())["smtp"]["password_enc"]

    resp = await client.put(
        "/api/settings",
        json={"smtp": {"host": "x", "password": None}},
    )
    assert resp.status == 200
    updated_enc = json.loads((tmp_base / "settings.json").read_text())["smtp"]["password_enc"]
    assert updated_enc == original_enc


@pytest.mark.asyncio
async def test_put_smtp_password_empty_clears_existing(client, tmp_base):
    """Explicit `password: ""` clears the stored ciphertext."""
    await client.put("/api/settings", json={"smtp": {"password": "original"}})
    assert json.loads((tmp_base / "settings.json").read_text())["smtp"]["password_enc"] != ""

    resp = await client.put(
        "/api/settings",
        json={"smtp": {"password": ""}},
    )
    assert resp.status == 200

    raw = json.loads((tmp_base / "settings.json").read_text())
    assert raw["smtp"]["password_enc"] == ""

    resp2 = await client.get("/api/settings")
    data = await resp2.json()
    assert data["smtp"]["password_set"] is False


@pytest.mark.asyncio
async def test_put_smtp_password_wrong_type_returns_400(client):
    """smtp.password as a number or array is rejected with 400."""
    resp = await client.put(
        "/api/settings",
        json={"smtp": {"password": 12345}},
    )
    assert resp.status == 400


@pytest.mark.asyncio
async def test_put_smtp_password_enc_directly_is_rejected(client, tmp_base):
    """SECURITY: a client cannot set smtp.password_enc directly — that
    would bypass Fernet encryption and let plaintext (or arbitrary
    garbage) land in the field that's supposed to be ciphertext.
    The server must reject the request with 400 before writing
    anything to disk."""
    # Pre-set a legitimate encrypted password so we can prove the
    # rejected request didn't overwrite it.
    await client.put("/api/settings", json={"smtp": {"password": "legitimate"}})
    before = json.loads((tmp_base / "settings.json").read_text())["smtp"]["password_enc"]
    assert before != ""

    # Attempt to inject plaintext as if it were ciphertext
    resp = await client.put(
        "/api/settings",
        json={"smtp": {"password_enc": "plaintext-attacker-controlled"}},
    )
    assert resp.status == 400
    data = await resp.json()
    assert "password_enc" in data["error"]

    # Disk state must be unchanged — the rejection happens before
    # the deep-merge + save path.
    after = json.loads((tmp_base / "settings.json").read_text())["smtp"]["password_enc"]
    assert after == before


# ─── Cross-origin defense ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_cross_origin_is_rejected(client):
    """A browser tab on a different domain PUTting settings is rejected
    with 403. The same-origin check compares Origin header's host
    against the Host header."""
    resp = await client.put(
        "/api/settings",
        json={"power": {"rate_per_kwh": 99}},
        headers={
            "Origin": "https://evil.example.com",
            "Host": "gpu-monitor.local",
        },
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_put_same_origin_with_default_port_is_accepted(client):
    """Origin header with no port (implying default 443 for https)
    and Host header with explicit :443 should compare equal. This
    edge case used to fail when the comparison was raw string
    equality between Origin.split('//')[1] and Host — the 443 /
    no-443 asymmetry tripped legitimate same-origin requests."""
    resp = await client.put(
        "/api/settings",
        json={"power": {"rate_per_kwh": 0.12}},
        headers={
            "Origin": "https://gpu-monitor.local",  # implied port 443
            "Host": "gpu-monitor.local:443",        # explicit port 443
        },
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_put_same_origin_with_different_case_is_accepted(client):
    """Host comparison must be case-insensitive."""
    resp = await client.put(
        "/api/settings",
        json={"power": {"rate_per_kwh": 0.12}},
        headers={
            "Origin": "http://GPU-Monitor.Local:8081",
            "Host": "gpu-monitor.local:8081",
        },
    )
    assert resp.status == 200


@pytest.mark.asyncio
async def test_put_same_origin_ipv6_bracketed_is_accepted(client):
    """IPv6 bracketed host forms ([::1]:8081) must work for same-
    origin comparison."""
    resp = await client.put(
        "/api/settings",
        json={"power": {"rate_per_kwh": 0.12}},
        headers={
            "Origin": "http://[::1]:8081",
            "Host": "[::1]:8081",
        },
    )
    assert resp.status == 200


# ─── SMTP test endpoint (Phase 6b real implementation) ─────────────────────


@pytest.mark.asyncio
async def test_smtp_test_with_empty_host_returns_400(client):
    """Phase 6b real impl — no SMTP host configured → 400 with a
    clear message pointing at the Settings view."""
    resp = await client.post("/api/settings/smtp/test")
    assert resp.status == 400
    data = await resp.json()
    assert data["ok"] is False
    assert "not configured" in data["error"]
