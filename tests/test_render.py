"""
Smoke tests for reporting.render.

Scope:
  * generate_report returns a MIME multipart/alternative with
    text/plain primary + text/html alternative
  * The HTML alternative carries N*2 multipart/related PNG images
    (one temperature + one power per GPU in the seeded fixture)
  * Every <img src="cid:..."> in the HTML matches a Content-ID
    on a related part (no dead references)
  * Empty DB still produces a valid message (no matplotlib crash)
  * include_charts=False skips the matplotlib + CID path entirely
  * RenderError is raised for unknown template strings

Runs in the same python:3.11-slim container as the rest of the
suite; matplotlib is the heaviest pip install in this file's
dependency chain (~30 MB of numpy + matplotlib wheels).
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
from reporting import render  # noqa: E402


# ─── Fixture ───────────────────────────────────────────────────────────────


@pytest.fixture
def render_env(tmp_path):
    """Create a stand-in /app with VERSION, gpu_inventory.json (2
    synthetic GPUs), and a SQLite DB seeded with 50 rows per GPU
    spread over the last hour. Returns a dict of paths ready to
    hand to generate_report()."""
    base = tmp_path / "app"
    base.mkdir()
    history = base / "history"
    history.mkdir()

    (base / "VERSION").write_text("1.0.0-test\n")

    inventory = {
        "gpus": [
            {
                "index": 0, "name": "Test Card A",
                "uuid": "GPU-A", "memory_total_mib": 24576,
                "power_limit_w": 450,
            },
            {
                "index": 1, "name": "Test Card B",
                "uuid": "GPU-B", "memory_total_mib": 16384,
                "power_limit_w": 320,
            },
        ],
    }
    inv_path = base / "gpu_inventory.json"
    inv_path.write_text(json.dumps(inventory))

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
    for gpu_index, base_temp, base_power in [(0, 55.0, 280.0), (1, 48.0, 150.0)]:
        for i in range(50):
            ts_epoch = now - (49 - i) * 60  # spread over 50 minutes
            ts_str = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """
                INSERT INTO gpu_metrics
                (timestamp, timestamp_epoch, temperature, utilization, memory, power,
                 gpu_index, gpu_uuid, interval_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 4)
                """,
                (ts_str, ts_epoch, base_temp + i * 0.1, 50.0, 8192.0,
                 base_power + i * 0.5, gpu_index, f"GPU-{gpu_index}"),
            )
    conn.commit()
    conn.close()

    # Minimal settings.json with rate_per_kwh set so the cost tile
    # renders a non-zero value and the aggregate path gets exercised.
    settings_path = base / "settings.json"
    settings_path.write_text(json.dumps({
        "power": {"rate_per_kwh": 0.15, "currency": "$"},
    }))

    return {
        "db_file": db_path,
        "inventory_file": inv_path,
        "settings_file": settings_path,
        "version": "1.0.0-test",
    }


# ─── Tests ─────────────────────────────────────────────────────────────────


def test_daily_report_returns_multipart_alternative(render_env):
    """generate_report produces a MIME message with text/plain and
    text/html alternatives. The HTML alternative has embedded PNG
    images as multipart/related subparts."""
    msg = render.generate_report(template="daily", **render_env)
    assert isinstance(msg, EmailMessage)
    assert msg.get_content_type() == "multipart/alternative"
    assert msg["Subject"] == "GPU Monitor daily report"

    # Walk subparts — should find text/plain, multipart/related,
    # text/html (inside related), and at least 4 image/png
    # (2 charts × 2 GPUs).
    types = [part.get_content_type() for part in msg.walk()]
    assert "text/plain" in types
    assert "text/html" in types
    assert "multipart/related" in types

    png_count = sum(1 for t in types if t == "image/png")
    assert png_count == 4  # 2 temp + 2 power


def test_every_cid_reference_has_matching_content_id(render_env):
    """No dead <img src='cid:...'> references. The CID in the HTML
    must match a Content-ID header on a related PNG part."""
    msg = render.generate_report(template="daily", **render_env)

    # Extract all cid: references from the HTML body
    html_parts = [
        p for p in msg.walk() if p.get_content_type() == "text/html"
    ]
    assert len(html_parts) == 1
    html_body = html_parts[0].get_content()
    cid_refs = set(re.findall(r'src="cid:([^"]+)"', html_body))
    assert len(cid_refs) > 0, "HTML has no cid: references — charts missing?"

    # Extract Content-ID headers from related image parts
    image_cids = set()
    for part in msg.walk():
        if part.get_content_type().startswith("image/"):
            cid_header = part.get("Content-ID")
            if cid_header:
                # Content-ID is wrapped in <...>
                image_cids.add(cid_header.strip("<>"))

    missing = cid_refs - image_cids
    assert not missing, f"HTML references CIDs that have no image part: {missing}"
    orphaned = image_cids - cid_refs
    assert not orphaned, f"Image parts with CIDs that nothing references: {orphaned}"


def test_empty_db_renders_without_crashing(tmp_path):
    """Fresh DB with zero rows still produces a valid message —
    matplotlib's empty-data path has historically crashed on
    plt.plot([], [])."""
    base = tmp_path / "app"
    base.mkdir()
    (base / "history").mkdir()

    # Empty but valid DB
    db_path = base / "history" / "gpu_metrics.db"
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
    conn.commit()
    conn.close()

    inv_path = base / "gpu_inventory.json"
    inv_path.write_text(json.dumps({"gpus": [{"index": 0, "name": "Solo"}]}))

    settings_path = base / "settings.json"
    settings_path.write_text("{}")

    msg = render.generate_report(
        template="daily",
        db_file=db_path,
        inventory_file=inv_path,
        settings_file=settings_path,
        version="0.0.0-empty",
    )
    assert isinstance(msg, EmailMessage)
    # Empty DB → the placeholder charts still get attached (2
    # charts × 1 GPU = 2 PNGs) and the HTML is valid
    png_count = sum(
        1 for p in msg.walk() if p.get_content_type() == "image/png"
    )
    assert png_count == 2


def test_include_charts_false_skips_images(render_env):
    """include_charts=False skips matplotlib and produces an HTML-only
    message with no multipart/related section. Used by the preview
    endpoint which streams HTML to an iframe."""
    msg = render.generate_report(
        template="daily", include_charts=False, **render_env,
    )
    types = [part.get_content_type() for part in msg.walk()]
    assert "text/html" in types
    # No image parts, no multipart/related
    assert "image/png" not in types
    assert "multipart/related" not in types


def test_unknown_template_raises_render_error(render_env):
    """Template strings outside the allowed set raise RenderError."""
    with pytest.raises(render.RenderError, match="unknown template"):
        render.generate_report(template="quarterly", **render_env)


def test_weekly_template_window(render_env):
    """Weekly template uses 7d window — render should succeed and
    produce a valid message."""
    msg = render.generate_report(template="weekly", **render_env)
    assert msg["Subject"] == "GPU Monitor weekly report"
