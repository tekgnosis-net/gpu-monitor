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
    GET  /api/stats/power?range=24h&gpu=0     integrated energy + power stats
    GET  /api/settings                        current settings.json (pw redacted)
    PUT  /api/settings                        partial-merge update with validation
    POST /api/settings/smtp/test              send a test email with current config

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
import sqlite3
import sys
from pathlib import Path

from aiohttp import web

# Allow `from reporting import ...` even when server.py is run via
# `python3 /app/server.py` rather than as part of a package. The
# Dockerfile lays out /app with server.py at the root and reporting/
# as a sibling directory; adding the parent directory (BASE_DIR) to
# sys.path mirrors how the module discovery would work if we had a
# setup.py or pyproject.toml in the image.
_SERVER_DIR = Path(__file__).resolve().parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from reporting import crypto  # noqa: E402
from reporting.settings import (  # noqa: E402
    DEFAULT_SETTINGS,
    Settings,
    load_settings,
    save_settings,
)
from pydantic import ValidationError  # noqa: E402


BASE_DIR = Path("/app")
VERSION_FILE = BASE_DIR / "VERSION"
INVENTORY_FILE = BASE_DIR / "gpu_inventory.json"
DB_FILE = BASE_DIR / "history" / "gpu_metrics.db"
SETTINGS_FILE = BASE_DIR / "settings.json"
SECRET_KEY_FILE = BASE_DIR / "history" / ".secret"
SCHEMA_VERSION = 2  # Matches Phase 1 migration (gpu_index, gpu_uuid, interval_s)

# Allowed `range` query parameter values and their durations in seconds.
# Two dicts intentionally, not one — the set of legal ranges differs per
# endpoint:
#
#   * HISTORY_RANGE_SECONDS is used by /api/metrics/history, which returns
#     every sampled row in the window. 30 days × 4s sampling × 2 GPUs is
#     ~1.3M points — enough to freeze the browser and slam SQLite. The
#     history endpoint caps at 7d.
#
#   * POWER_RANGE_SECONDS is used by /api/stats/power, which returns a
#     single SUM across the window. 30 days is fine because the cost
#     for the aggregation is O(n) over an index-served range scan and
#     the response shape is a handful of floats regardless of window.
#
# Shared entries are duplicated rather than computed via set operations
# so a future change (e.g. removing 7d for some reason) touches the
# endpoint whose behavior is actually changing.
HISTORY_RANGE_SECONDS: dict[str, int] = {
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 1 * 3600,
    "6h": 6 * 3600,
    "12h": 12 * 3600,
    "24h": 24 * 3600,
    "3d": 3 * 86400,
    "7d": 7 * 86400,
}
POWER_RANGE_SECONDS: dict[str, int] = {
    **HISTORY_RANGE_SECONDS,
    "30d": 30 * 86400,
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


def _resolve_range(param: str | None, allowlist: dict[str, int]) -> tuple[int, str]:
    """Translate a range query-parameter (e.g. '24h', '3d') into
    (seconds, effective_key) against the given allowlist, defaulting to
    24h when missing or unrecognized.

    Returning both lets the response include the *effective* key (what
    the query actually used) rather than whatever raw string the client
    sent. This avoids a class of client-side confusion where a partial
    or invalid ?range=... produces response data for one window but the
    response metadata says another.

    A stricter alternative would 400 on unknown values; we prefer a
    friendly default so a stale frontend build never breaks the page.
    """
    if not param:
        return allowlist[DEFAULT_RANGE], DEFAULT_RANGE
    key = param.lower()
    if key in allowlist:
        return allowlist[key], key
    return allowlist[DEFAULT_RANGE], DEFAULT_RANGE


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
    frontend and can introduce a richer shape then.

    Phase 5 note: this endpoint deliberately uses HISTORY_RANGE_SECONDS
    (no 30d) because 30 days of 4s sampling is ~648k rows per GPU and
    returning every one as JSON would freeze the browser. The
    /api/stats/power endpoint accepts 30d because it only returns
    aggregate scalars, not raw rows."""
    range_s, _effective_range = _resolve_range(
        request.query.get("range"), HISTORY_RANGE_SECONDS
    )
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


async def handle_stats_power(request: web.Request) -> web.Response:
    """Integrated energy + power statistics for a single GPU over a time
    range. Used by the Phase 5 Power view to populate its per-window
    tiles (energy, peak, avg) and the cost calculation.

    The energy integration is a Riemann sum: for each sample we multiply
    the instantaneous power by the interval the sample represents, then
    divide by 3600 to convert watt-seconds to watt-hours. Storing
    `interval_s` per row in Phase 1 is what lets this stay correct when
    the user changes `collection.interval_seconds` mid-window — a row
    at 4 s contributes `power * 4`, a row at 10 s contributes
    `power * 10`. No `LAG()` gymnastics, one `SUM`, O(n) over windowed
    rows served directly from the composite (gpu_index, timestamp_epoch)
    index.

    Invalid samples (NULL power or power ≤ 0, which the collector writes
    when nvidia-smi reports [N/A] or the card lacks a usable sensor) are
    excluded from the integration — they'd otherwise contribute zero and
    silently under-count energy. Their count is returned alongside the
    total so the frontend can surface an "insufficient_telemetry" notice
    without hiding the tile.

    Response shape:
        {
          "range": "24h",
          "gpu_index": 0,
          "energy_wh": 1234.5,
          "peak_power_w": 280.0,
          "avg_power_w":  150.3,
          "samples_total": 21600,
          "samples_invalid": 0,
          "insufficient_telemetry": false
        }
    """
    range_s, effective_range = _resolve_range(
        request.query.get("range"), POWER_RANGE_SECONDS
    )
    gpu_index = _parse_gpu_param(request.query.get("gpu"))

    # Single fallback shape used for every failure path below so the
    # client always gets the same keys. `range` is the *effective*
    # (normalized) key — not the raw query string — so a partial/
    # unknown ?range=... doesn't confuse clients that display the
    # response metadata back to the user.
    def _empty_response() -> dict:
        return {
            "range": effective_range,
            "gpu_index": gpu_index,
            "energy_wh": 0.0,
            "peak_power_w": 0.0,
            "avg_power_w": 0.0,
            "samples_total": 0,
            "samples_invalid": 0,
            "insufficient_telemetry": True,
        }

    try:
        conn = _open_db_readonly()
    except sqlite3.OperationalError as exc:
        log.warning("stats_power: cannot open DB: %s", exc)
        return web.json_response(_empty_response(), status=200)

    try:
        try:
            # Single aggregation query — all metrics fall out of one
            # scan of the windowed rows for this GPU. The CASE WHEN
            # guards ensure NULL / non-positive power rows don't corrupt
            # peak/avg/energy but still get counted in samples_total via
            # an unconditional COUNT(*) so the frontend can compute the
            # invalid ratio.
            row = conn.execute(
                """
                SELECT
                    COALESCE(
                        SUM(CASE WHEN power > 0 THEN power * interval_s ELSE 0 END),
                        0
                    ) / 3600.0 AS energy_wh,
                    COALESCE(MAX(CASE WHEN power > 0 THEN power END), 0) AS peak_power_w,
                    COALESCE(AVG(CASE WHEN power > 0 THEN power END), 0) AS avg_power_w,
                    COUNT(*) AS samples_total,
                    SUM(CASE WHEN power IS NULL OR power <= 0 THEN 1 ELSE 0 END)
                        AS samples_invalid
                FROM gpu_metrics
                WHERE gpu_index = ?
                  AND timestamp_epoch > strftime('%s', 'now') - ?
                """,
                (gpu_index, range_s),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            # A DB-open that succeeded can still race a schema change,
            # WAL-checkpoint lock, or (in pathological cases) a missing
            # table. Match the DB-open fallback shape so callers see the
            # same "insufficient_telemetry: true" notice in both
            # failure modes rather than one silent 5xx and one graceful
            # placeholder.
            log.warning("stats_power: query failed: %s", exc)
            return web.json_response(_empty_response(), status=200)
    finally:
        conn.close()

    samples_total = int(row["samples_total"] or 0)
    samples_invalid = int(row["samples_invalid"] or 0)
    # "Insufficient telemetry" means either the window has no data at
    # all, or at least one sample had to be excluded from the energy
    # sum. The frontend shows a warning icon + tooltip when this is
    # true; the numeric values are still returned so partial data
    # remains visible.
    insufficient = samples_total == 0 or samples_invalid > 0

    return web.json_response({
        "range": effective_range,
        "gpu_index": gpu_index,
        "energy_wh": float(row["energy_wh"] or 0.0),
        "peak_power_w": float(row["peak_power_w"] or 0.0),
        "avg_power_w": float(row["avg_power_w"] or 0.0),
        "samples_total": samples_total,
        "samples_invalid": samples_invalid,
        "insufficient_telemetry": insufficient,
    })


# ─── Settings (Phase 6) ────────────────────────────────────────────────────


# Sentinel used by PUT /api/settings: smtp.password field semantics.
#
#   Field absent            → do not touch password_enc (preserve existing)
#   Field present = null    → same as absent (explicit "no change")
#   Field present = ""      → clear password_enc to ""
#   Field present = "X"     → encrypt "X" and store in password_enc
#
# The sentinel lets clients distinguish "I didn't send this field" from
# "I want to explicitly clear it". Without it, a PUT body containing
# everything *except* smtp.password would have to gymnastically preserve
# the existing ciphertext on the server side — this way the server just
# follows the rule table above.


def _redact_smtp_password(settings_dict: dict) -> dict:
    """Replace `smtp.password_enc` (ciphertext) with `smtp.password_set`
    (boolean) before sending settings to the client. The client should
    never see the ciphertext — even though it's encrypted, leaking it
    into browser DevTools / network logs would be an unnecessary
    attack surface. Display the boolean "is a password configured"
    instead, and let the user re-enter the plaintext in Settings if
    they want to change it."""
    redacted = json.loads(json.dumps(settings_dict))  # cheap deep copy
    smtp = redacted.get("smtp", {})
    password_enc = smtp.pop("password_enc", "")
    smtp["password_set"] = bool(password_enc)
    return redacted


def _origin_is_same(request: web.Request) -> bool:
    """Defense-in-depth CSRF check for mutating routes. aiohttp gives
    us Origin and Host headers; a same-origin request has them
    matching. Cross-origin requests (from a different host or a
    browser tab on another domain) have an Origin that the Host
    doesn't match, at which point we refuse. This is a LAN-only
    defense — it doesn't help against a malicious LAN client — but
    it costs 4 lines and prevents a drive-by click on a bookmark'd
    IP from mutating settings."""
    origin = request.headers.get("Origin")
    # Origin is absent on same-origin GETs and some browser contexts.
    # We only enforce when it's present; a None Origin is indistinguishable
    # from a direct curl and we don't want to break that for users.
    if not origin:
        return True
    host = request.headers.get("Host", "")
    # Origin is "scheme://host[:port]"; normalize to the host portion
    # and compare against the Host header's host portion.
    try:
        origin_host = origin.split("://", 1)[1]
    except IndexError:
        return False
    return origin_host == host


async def handle_get_settings(request: web.Request) -> web.Response:
    """Return current settings with SMTP password ciphertext redacted.

    Missing settings.json is a valid state — returns the DEFAULT_SETTINGS
    shape so first-run UIs render identically to post-save UIs.
    """
    try:
        data = load_settings(SETTINGS_FILE)
    except Exception as exc:  # pragma: no cover — load_settings swallows its own errors
        log.warning("get_settings: load failed: %s", exc)
        data = json.loads(json.dumps(DEFAULT_SETTINGS))

    return web.json_response(_redact_smtp_password(data))


async def handle_put_settings(request: web.Request) -> web.Response:
    """Partial-merge update: deep-merge the request body over the
    current settings, validate via Pydantic, encrypt smtp.password
    transition if present, atomically write.

    Errors returned as JSON:
        400 — body not JSON, or validation failure (with field detail)
        403 — same-origin check failed
        500 — filesystem / crypto failure
    """
    if not _origin_is_same(request):
        return web.json_response({"error": "cross-origin rejected"}, status=403)

    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    # Extract the SMTP password transition BEFORE the merge so the
    # deep-merge logic doesn't have to understand the sentinel. We
    # re-inject the encrypted form into the merged dict after
    # validation.
    smtp_body = body.get("smtp")
    plaintext_password: str | None
    clear_password = False
    if isinstance(smtp_body, dict) and "password" in smtp_body:
        raw = smtp_body.pop("password")
        if raw is None:
            plaintext_password = None   # absent / unchanged
        elif raw == "":
            plaintext_password = None   # still "no change to plaintext"
            clear_password = True       # but explicitly clear the ciphertext
        elif isinstance(raw, str):
            plaintext_password = raw
        else:
            return web.json_response(
                {"error": "smtp.password must be a string or null"}, status=400
            )
    else:
        plaintext_password = None

    # Deep-merge over the existing file, then validate the result.
    from reporting.settings import _deep_merge  # local import keeps tests happy

    current = load_settings(SETTINGS_FILE)
    merged = _deep_merge(current, body)

    try:
        validated = Settings.model_validate(merged)
    except ValidationError as exc:
        return web.json_response(
            {"error": "validation failed", "detail": exc.errors()},
            status=400,
        )

    validated_dict = validated.model_dump(by_alias=True)

    # Apply the SMTP password transition after validation (Pydantic
    # doesn't know about the plaintext field, only password_enc).
    if clear_password:
        validated_dict.setdefault("smtp", {})["password_enc"] = ""
    elif plaintext_password is not None:
        try:
            key = crypto.load_or_create_key(SECRET_KEY_FILE)
            validated_dict.setdefault("smtp", {})["password_enc"] = crypto.encrypt(
                plaintext_password, key
            )
        except crypto.CryptoError as exc:
            log.error("put_settings: encryption failed: %s", exc)
            return web.json_response(
                {"error": "encryption failed", "detail": str(exc)},
                status=500,
            )

    try:
        save_settings(SETTINGS_FILE, validated_dict)
    except OSError as exc:
        log.error("put_settings: write failed: %s", exc)
        return web.json_response(
            {"error": "could not persist settings", "detail": str(exc)},
            status=500,
        )

    return web.json_response(_redact_smtp_password(validated_dict))


async def handle_smtp_test(request: web.Request) -> web.Response:
    """Send a "Hello from GPU Monitor" test email using the *currently
    saved* SMTP config. Reports success/failure inline to the Settings
    view so the user gets immediate feedback on their configuration.

    Phase 6.1 stub: the actual mailer lives in 6.3 so this returns a
    501 Not Implemented until reporting.mailer is wired up. Flagged
    as stubbed rather than omitted so the route is registered, the
    URL pattern is stable, and the Settings view's "Test" button has
    something to call from day one.
    """
    if not _origin_is_same(request):
        return web.json_response({"error": "cross-origin rejected"}, status=403)
    return web.json_response(
        {
            "ok": False,
            "error": "SMTP mailer not yet implemented (Phase 6.3)",
        },
        status=501,
    )


# ─── Static ─────────────────────────────────────────────────────────────────

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
    app.router.add_get("/api/stats/power",     handle_stats_power)

    # Phase 6 — settings CRUD. GET is read-only and unauthenticated;
    # PUT and POST routes enforce the same-origin header check via
    # handler-level guards.
    app.router.add_get ("/api/settings",           handle_get_settings)
    app.router.add_put ("/api/settings",           handle_put_settings)
    app.router.add_post("/api/settings/smtp/test", handle_smtp_test)

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
    log.info("       /api/metrics/current, /api/metrics/history")
    log.info("       /api/stats/24h, /api/stats/power")
    log.info("       /api/settings (GET/PUT), /api/settings/smtp/test")
    log.info("========================================")
    web.run_app(make_app(), port=8081, access_log=None)
