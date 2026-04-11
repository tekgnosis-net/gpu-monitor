"""
GPU Monitor API server.

Phase 3 of the v1.0.0 overhaul. Previously this file was a 30-line static
file server; it now hosts a small aiohttp JSON API alongside the static
frontend. Routes:

    GET  /api/health                          liveness + version + schema
    GET  /api/version                         {version: "..."}
    GET  /api/gpus                            inventory array from gpu_inventory.json
    GET  /api/metrics/current                 latest sample per GPU (array)
    GET  /api/metrics/history?range=24h&gpu=0 timeseries for one GPU
    GET  /api/stats/24h                       per-GPU min/max array

The API reads from:
  * /app/VERSION             (single source of truth for version)
  * /app/gpu_inventory.json  (written by discover_gpus at startup)
  * /app/history/gpu_metrics.db  (SQLite, WAL mode)

All SQLite reads open a fresh read-only connection per request. WAL mode
(enabled in Phase 1) allows concurrent readers alongside the collector's
write path — no connection pool or lock coordination needed.

Static file serving remains on a catch-all at /{tail:.*} so the existing
gpu-stats.html, images/, sounds/ paths still work. API routes are
registered BEFORE the static catch-all so the longer /api/* prefix wins
the aiohttp route match.

Phase 3 deliberately sticks with aiohttp rather than swapping to FastAPI:
the existing supervisor pattern (run_web_server in monitor_gpu.sh) already
wraps this process, all helper code is in stdlib-only Python, and
introducing a second framework plus pydantic would broaden the container
dep surface for no Phase 3 user-visible gain. Settings persistence in
Phase 6 will add PUT routes and MAY justify pulling in pydantic at that
point; Phase 3 does not.
"""

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from aiohttp import web


BASE_DIR = Path("/app")
VERSION_FILE = BASE_DIR / "VERSION"
INVENTORY_FILE = BASE_DIR / "gpu_inventory.json"
DB_FILE = BASE_DIR / "history" / "gpu_metrics.db"
SCHEMA_VERSION = 2  # Matches Phase 1 migration (gpu_index, gpu_uuid, interval_s)

# Allowed `range` query parameter values and their durations in seconds.
# Keep this tight — the frontend picks from a fixed set of timeframe
# buttons (15m, 30m, 1h, 6h, 12h, 24h, 3d, 7d) and free-form values would
# just invite confusion.
RANGE_SECONDS: dict[str, int] = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 1 * 3600,
    "6h": 6 * 3600,
    "12h": 12 * 3600,
    "24h": 24 * 3600,
    "3d": 3 * 86400,
    "7d": 7 * 86400,
}
DEFAULT_RANGE = "24h"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gpu-monitor")


# ─── Helpers ────────────────────────────────────────────────────────────────

def _read_version() -> str:
    """Read the tracked application version. Returns 'unknown' on any error
    (missing file, permission denied, etc.) rather than failing — the API
    should report an unknown version, not return a 500."""
    try:
        return VERSION_FILE.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _read_inventory() -> list[dict]:
    """Load the multi-GPU inventory written by discover_gpus at startup.
    Returns an empty list on any error; callers decide whether that is an
    expected fallback or a 5xx-worthy condition."""
    try:
        data = json.loads(INVENTORY_FILE.read_text())
        return list(data.get("gpus", []))
    except (OSError, ValueError):
        return []


def _open_db_readonly() -> sqlite3.Connection:
    """Open a read-only connection to the metrics database. Read-only mode
    prevents any accidental writes from the API path — the collector is
    the only writer, always, and this boundary should be enforced in code
    rather than relying on reviewer discipline."""
    uri = f"file:{DB_FILE}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _range_seconds(param: str | None) -> int:
    """Translate a range query-parameter (e.g. '24h', '3d') into seconds,
    defaulting to 24h when missing or unrecognized. A stricter approach
    would 400 on unknown values; for Phase 3 we prefer a friendly default
    over breaking the dashboard if the frontend sends something unexpected."""
    if not param:
        return RANGE_SECONDS[DEFAULT_RANGE]
    return RANGE_SECONDS.get(param.lower(), RANGE_SECONDS[DEFAULT_RANGE])


def _parse_gpu_param(param: str | None) -> int:
    """Parse the ?gpu=N query parameter. Returns 0 on missing/invalid —
    the legacy frontend always wants GPU 0 and Phase 3's retrofit doesn't
    change that expectation. Multi-GPU selection is a Phase 4 concern."""
    if param is None:
        return 0
    try:
        return max(0, int(param))
    except ValueError:
        return 0


# ─── Route handlers ─────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    """Liveness probe + quick sanity info. Cheap enough to call often."""
    return web.json_response({
        "ok": True,
        "version": _read_version(),
        "schema": SCHEMA_VERSION,
    })


async def handle_version(request: web.Request) -> web.Response:
    """Just the version string. Separate from /api/health for consumers
    (e.g. the sidebar footer in Phase 4) that only care about the version."""
    return web.json_response({"version": _read_version()})


async def handle_gpus(request: web.Request) -> web.Response:
    """Returns the `gpus` array from the inventory JSON. An empty array is
    a valid response for a dev/test container without nvidia-smi — the
    synthetic single-GPU fallback in discover_gpus always writes at least
    one entry, but if the file is somehow missing we still return 200
    with [] rather than 5xx."""
    return web.json_response({"gpus": _read_inventory()})


async def handle_metrics_current(request: web.Request) -> web.Response:
    """Latest sample per GPU. Uses a correlated subquery to find the most
    recent timestamp_epoch per gpu_index, avoiding a GROUP BY that SQLite's
    query planner would have to resolve with per-group scans."""
    try:
        conn = _open_db_readonly()
    except sqlite3.OperationalError as exc:
        log.warning("metrics_current: cannot open DB: %s", exc)
        return web.json_response([], status=200)

    try:
        rows = conn.execute("""
            SELECT m.gpu_index, m.gpu_uuid, m.timestamp,
                   m.temperature, m.utilization, m.memory, m.power
            FROM gpu_metrics m
            WHERE m.timestamp_epoch = (
                SELECT MAX(timestamp_epoch)
                FROM gpu_metrics m2
                WHERE m2.gpu_index = m.gpu_index
            )
            ORDER BY m.gpu_index ASC
        """).fetchall()
    finally:
        conn.close()

    return web.json_response([
        {
            "gpu_index": r["gpu_index"],
            "gpu_uuid": r["gpu_uuid"],
            "timestamp": r["timestamp"],
            "temperature": r["temperature"],
            "utilization": r["utilization"],
            "memory": r["memory"],
            "power": r["power"],
        }
        for r in rows
    ])


async def handle_metrics_history(request: web.Request) -> web.Response:
    """Timeseries for a single GPU over a time range. Shape matches the
    legacy history/history.json contract exactly so the Phase 3 retrofit
    of gpu-stats.html is a pure URL change — Phase 4 rewrites the
    frontend and can introduce a richer shape then."""
    range_s = _range_seconds(request.query.get("range"))
    gpu_index = _parse_gpu_param(request.query.get("gpu"))

    try:
        conn = _open_db_readonly()
    except sqlite3.OperationalError as exc:
        log.warning("metrics_history: cannot open DB: %s", exc)
        return web.json_response({
            "timestamps": [], "temperatures": [], "utilizations": [],
            "memory": [], "power": [],
        }, status=200)

    try:
        cutoff_sql = "strftime('%s', 'now') - ?"
        rows = conn.execute(
            f"""
            SELECT timestamp, temperature, utilization, memory, power
            FROM gpu_metrics
            WHERE gpu_index = ? AND timestamp_epoch > ({cutoff_sql})
            ORDER BY timestamp_epoch ASC
            """,
            (gpu_index, range_s),
        ).fetchall()
    finally:
        conn.close()

    return web.json_response({
        "timestamps":   [r["timestamp"]    for r in rows],
        "temperatures": [r["temperature"]  for r in rows],
        "utilizations": [r["utilization"]  for r in rows],
        "memory":       [r["memory"]       for r in rows],
        "power":        [r["power"]        for r in rows],
    })


async def handle_stats_24h(request: web.Request) -> web.Response:
    """Per-GPU min/max over the last 24 hours. The response wraps each GPU
    entry in a {stats: {...}} object so the shape is structurally
    identical to the legacy gpu_24hr_stats.txt contract — the retrofitted
    frontend just picks [0].stats for the single-GPU legacy view. Future
    multi-GPU frontends in Phase 4 can index by gpu_index."""
    try:
        conn = _open_db_readonly()
    except sqlite3.OperationalError as exc:
        log.warning("stats_24h: cannot open DB: %s", exc)
        return web.json_response([], status=200)

    try:
        rows = conn.execute("""
            SELECT
                gpu_index,
                MIN(temperature) AS temp_min, MAX(temperature) AS temp_max,
                MIN(utilization) AS util_min, MAX(utilization) AS util_max,
                MIN(memory) AS mem_min,       MAX(memory) AS mem_max,
                MIN(CASE WHEN power > 0 THEN power ELSE NULL END) AS power_min,
                MAX(power) AS power_max
            FROM gpu_metrics
            WHERE timestamp_epoch > strftime('%s', 'now') - 86400
            GROUP BY gpu_index
            ORDER BY gpu_index ASC
        """).fetchall()
    finally:
        conn.close()

    return web.json_response([
        {
            "gpu_index": r["gpu_index"],
            "stats": {
                "temperature": {"min": r["temp_min"] or 0, "max": r["temp_max"] or 0},
                "utilization": {"min": r["util_min"] or 0, "max": r["util_max"] or 0},
                "memory":      {"min": r["mem_min"]  or 0, "max": r["mem_max"]  or 0},
                "power":       {"min": r["power_min"] or 0, "max": r["power_max"] or 0},
            },
        }
        for r in rows
    ])


async def handle_static(request: web.Request) -> web.Response:
    """Catch-all static file serving for the legacy frontend and assets.
    Registered LAST in the route table so /api/* prefixes take precedence.
    Uses BASE_DIR as the root and falls back to gpu-stats.html at /."""
    rel = request.path.lstrip("/")
    if not rel:
        rel = "gpu-stats.html"

    # Safety: resolve the requested path against BASE_DIR and reject any
    # traversal that escapes the root. aiohttp's router already decodes
    # percent-escapes before we get here, but defense-in-depth is cheap.
    target = (BASE_DIR / rel).resolve()
    try:
        target.relative_to(BASE_DIR.resolve())
    except ValueError:
        return web.Response(status=403)

    if target.is_file():
        return web.FileResponse(target)
    return web.Response(status=404)


# ─── App construction ──────────────────────────────────────────────────────

def make_app() -> web.Application:
    """Build the aiohttp Application. Extracted from the module-level
    so that tests/test_api.py can construct a fresh instance per test
    without running the server."""
    app = web.Application()

    # API routes FIRST — aiohttp matches in registration order, so the
    # more specific /api/* prefixes have to come before the catch-all.
    app.router.add_get("/api/health",          handle_health)
    app.router.add_get("/api/version",         handle_version)
    app.router.add_get("/api/gpus",            handle_gpus)
    app.router.add_get("/api/metrics/current", handle_metrics_current)
    app.router.add_get("/api/metrics/history", handle_metrics_history)
    app.router.add_get("/api/stats/24h",       handle_stats_24h)

    # Static catch-all LAST.
    app.router.add_get("/{tail:.*}", handle_static)

    return app


if __name__ == "__main__":
    log.info("========================================")
    log.info("Starting NVIDIA GPU Monitor")
    log.info("https://github.com/tekgnosis-net/gpu-monitor")
    log.info("----------------------------------------")
    log.info("Server running on: http://localhost:8081")
    log.info("  API: /api/health, /api/version, /api/gpus")
    log.info("       /api/metrics/current, /api/metrics/history, /api/stats/24h")
    log.info("========================================")
    web.run_app(make_app(), port=8081, access_log=None)
