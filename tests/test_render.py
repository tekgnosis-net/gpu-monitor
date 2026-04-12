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


def _extract_html(msg):
    """Pull the text/html part out of an EmailMessage. Helper for the
    preview-theme tests which all need to poke at the HTML body."""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            return part.get_payload(decode=True).decode("utf-8")
    return None


def test_preview_theme_dark_injects_override_style(render_env):
    """preview_theme='dark' appends a post-premailer <style> block
    that overrides the light-mode inlined styles with Apple HIG dark
    palette colors. This is required for the dashboard's dark-mode
    iframe preview; real email sends use preview_theme='none' so
    recipients see the designed palette.

    The crucial invariant is that cards (#2c2c2e) are LIGHTER than
    body (#1c1c1e) — CSS filter inversion reversed this hierarchy
    because it preserves relative brightness ordering. Server-side
    injection is the only way to restore the visual-depth convention
    dark-mode UX relies on."""
    msg = render.generate_report(
        template="daily",
        include_charts=False,
        preview_theme="dark",
        **render_env,
    )
    html = _extract_html(msg)
    assert html is not None
    assert "preview-dark-override" in html
    # The light-mode preview override must NOT leak in.
    assert "preview-light-override" not in html
    # Body is dark, cards are lighter than body, tiles are lighter
    # than cards — the three-level depth hierarchy.
    assert "#1c1c1e" in html  # body
    assert "#2c2c2e" in html  # elevated surface (cards)
    assert "#3a3a3c" in html  # deepest tile


def test_preview_theme_light_injects_override_style(render_env):
    """preview_theme='light' strengthens the authored template's
    body/card contrast so card boundaries remain visible in the
    preview iframe. The authored template uses #f5f5f7 body / #ffffff
    cards — ~3% brightness delta — which works in email clients
    (they render against their own white body so the #f5f5f7
    disappears) but washes out in the always-visible iframe preview.

    The override darkens body to #ebebf0 (~5% delta vs #ffffff cards),
    re-skins tiles to #f5f5f7 for visible separation from cards, and
    adds a soft shadow + stronger border for a crisper elevation cue.
    This is a preview-only approximation — real email sends use
    preview_theme='none' and receive the authored palette."""
    msg = render.generate_report(
        template="daily",
        include_charts=False,
        preview_theme="light",
        **render_env,
    )
    html = _extract_html(msg)
    assert html is not None
    assert "preview-light-override" in html
    # The dark-mode preview override must NOT leak in.
    assert "preview-dark-override" not in html
    # The preview-only light palette values.
    assert "#ebebf0" in html  # darkened body
    assert "#ffffff" in html  # cards unchanged
    assert "box-shadow" in html  # soft elevation shadow


def test_preview_theme_none_is_authored_template(render_env):
    """preview_theme='none' (the default for real email sends called
    from scheduler.py and the run-now endpoint) does NOT inject
    either override block. Recipients must see the authored template
    exactly — no preview approximations, no dashboard-themed tweaks,
    no shadow-on-cards hack. This test is a guardrail against any
    future refactor that accidentally flips the default."""
    msg = render.generate_report(
        template="daily",
        include_charts=False,
        # preview_theme omitted — uses the "none" default
        **render_env,
    )
    html = _extract_html(msg)
    assert html is not None
    assert "preview-dark-override" not in html
    assert "preview-light-override" not in html
    # The preview-only color values must NOT appear anywhere.
    assert "#1c1c1e" not in html  # dark-mode body
    assert "#2c2c2e" not in html  # dark-mode card
    assert "#ebebf0" not in html  # light-preview darkened body


def test_unknown_template_raises_render_error(render_env):
    """Template strings outside the allowed set raise RenderError."""
    with pytest.raises(render.RenderError, match="unknown template"):
        render.generate_report(template="quarterly", **render_env)


def test_weekly_template_window(render_env):
    """Weekly template uses 7d window — render should succeed and
    produce a valid message."""
    msg = render.generate_report(template="weekly", **render_env)
    assert msg["Subject"] == "GPU Monitor weekly report"
