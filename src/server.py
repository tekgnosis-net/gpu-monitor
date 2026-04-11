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
    GET  /api/housekeeping/db-info            size, row count, oldest/newest, per-GPU
    POST /api/housekeeping/vacuum             run SQLite VACUUM, return freed bytes
    POST /api/housekeeping/purge              delete rows older than N days
    POST /api/schedules/{id}/run-now          synchronously fire one report schedule
    GET  /api/reports/preview?template=daily  render a report body for iframe preview

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

from reporting import crypto, mailer, render  # noqa: E402
from reporting.settings import (  # noqa: E402
    DEFAULT_SETTINGS,
    Settings,
    deep_merge,
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


def _open_db_readwrite() -> sqlite3.Connection:
    """Open a read/write connection to the metrics database. Used only
    by the Phase 6 housekeeping routes (VACUUM, purge). The collector
    remains the only writer of measurements; these routes mutate
    *user-triggered* housekeeping operations on the same data.
    Longer timeout than the read-only path because VACUUM can be
    slow on large databases — SQLite will hold a write lock the
    whole time."""
    uri = f"file:{DB_FILE}?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0)
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


def _normalize_host_port(raw: str) -> tuple[str, str]:
    """Split a `host[:port]` string into (host, port), handling
    IPv6 brackets and case-insensitive host comparison.

    Returns ("", "") on malformed input. Used by _origin_is_same
    to produce a canonical (host, port) pair that can be compared
    across Origin / Host headers regardless of cosmetic
    differences (case, bracketing, port representation).
    """
    if not raw:
        return ("", "")
    raw = raw.strip()
    # IPv6 bracketed form: [::1]:8080 or [::1]
    if raw.startswith("["):
        close = raw.find("]")
        if close == -1:
            return ("", "")
        host = raw[1:close].lower()
        rest = raw[close + 1:]
        port = rest[1:] if rest.startswith(":") else ""
        return (host, port)
    # Plain form: host[:port]
    if ":" in raw:
        host, _, port = raw.rpartition(":")
        return (host.lower(), port)
    return (raw.lower(), "")


# Default ports per URL scheme. Used by the Origin/Host normalizer
# below so an Origin of "http://foo" (no port) compares equal to a
# Host of "foo:80" (explicit default port).
_DEFAULT_PORT = {"http": "80", "https": "443"}


def _origin_is_same(request: web.Request) -> bool:
    """Defense-in-depth CSRF check for mutating routes. aiohttp gives
    us Origin and Host headers; a same-origin request has them
    matching after canonicalization.

    Canonicalization handles three edge cases:

      1. **Case-insensitive host comparison.** "Example.com" and
         "example.com" are the same host.
      2. **IPv6 bracket handling.** An Origin of "[::1]:8080" must
         compare equal to a Host of "[::1]:8080", and the brackets
         must be stripped before comparison.
      3. **Default port normalization.** Origin "http://example.com"
         (no port → default 80) must compare equal to
         Host "example.com:80", and both should also equal Host
         "example.com" if the scheme is known.

    Origin is absent on same-origin GETs and direct curl calls. We
    only enforce when it's present; a None Origin is indistinguishable
    from a direct client and rejecting on its absence would break
    legitimate CLI usage.

    This is a LAN-only defense — it doesn't stop a malicious LAN
    client — but it costs a few lines and prevents a drive-by click
    on a bookmark'd IP from mutating settings via a malicious page
    in another tab.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True

    host_header = request.headers.get("Host", "")
    if not host_header:
        return False

    # Parse the Origin with urlparse so we get scheme + netloc + port
    # without hand-rolling the split. `parsed.hostname` lowercases
    # and strips IPv6 brackets; `parsed.port` returns an int or None.
    try:
        from urllib.parse import urlparse
        parsed = urlparse(origin)
    except ValueError:
        return False

    origin_host = (parsed.hostname or "").lower()
    origin_port = str(parsed.port) if parsed.port else _DEFAULT_PORT.get(parsed.scheme, "")

    host_host, host_port = _normalize_host_port(host_header)
    # If the Host header lacked a port, fill in the scheme default
    # so the comparison is apples-to-apples.
    if not host_port:
        host_port = _DEFAULT_PORT.get(parsed.scheme, "")

    return origin_host == host_host and origin_port == host_port


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

    # SECURITY: reject PUT bodies that contain `smtp.password_enc`.
    # That field is the ciphertext — it must only ever be computed
    # server-side from the `smtp.password` plaintext transition
    # below and the existing on-disk value. A client that PUTs a
    # raw `password_enc` would otherwise bypass Fernet encryption
    # entirely and persist plaintext or arbitrary junk into the
    # field that claims to be encrypted.
    #
    # Rejecting with 400 (rather than silently stripping) makes the
    # attack attempt observable in both the response body and the
    # server log. Legitimate clients never need to set this field
    # because GET /api/settings redacts it to `password_set: bool`
    # — any round-trip of a GET'd settings object will not contain
    # password_enc, so no legitimate workflow breaks.
    smtp_body = body.get("smtp")
    if isinstance(smtp_body, dict) and "password_enc" in smtp_body:
        return web.json_response(
            {
                "error": "smtp.password_enc is server-computed and cannot be set directly",
                "hint": "use smtp.password to set or clear the plaintext",
            },
            status=400,
        )

    # Extract the SMTP password transition BEFORE the merge so the
    # deep-merge logic doesn't have to understand the sentinel. We
    # re-inject the encrypted form into the merged dict after
    # validation.
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
    # deep_merge is the public helper from reporting.settings (the
    # underscore-prefixed `_deep_merge` is a back-compat alias and
    # should not be used from new code — the Copilot round 2 review
    # correctly flagged the original import as private-API coupling).
    current = load_settings(SETTINGS_FILE)
    merged = deep_merge(current, body)

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

    Phase 6b real implementation: reads settings.json, decrypts
    smtp.password_enc, builds a minimal test message via
    mailer.build_test_message, hands it to mailer.send_message.

    Recipients: by default the test sends to the configured user
    address (same as "email yourself"). Callers can override via
    a POST body `{"to": "alice@example.com"}` to send to a
    different inbox — useful for verifying the relay can reach
    external addresses without having to first set a separate
    `user` account.
    """
    if not _origin_is_same(request):
        return web.json_response({"error": "cross-origin rejected"}, status=403)

    # Optional body with alternate recipient
    override_to: str | None = None
    if request.can_read_body:
        try:
            body = await request.json()
            if isinstance(body, dict) and isinstance(body.get("to"), str):
                override_to = body["to"]
        except (ValueError, json.JSONDecodeError):
            # Empty / non-JSON body is fine — we default to user=from
            pass

    data = load_settings(SETTINGS_FILE)
    smtp = data.get("smtp", {})
    host = smtp.get("host") or ""
    if not host:
        return web.json_response(
            {
                "ok": False,
                "error": "SMTP host is not configured. Set it in Settings → SMTP.",
            },
            status=400,
        )

    from_addr = smtp.get("from") or smtp.get("user") or "gpu-monitor@localhost"
    to_addr = override_to or smtp.get("user") or from_addr

    try:
        key = crypto.load_or_create_key(SECRET_KEY_FILE)
        plaintext = crypto.decrypt(smtp.get("password_enc", ""), key)
    except crypto.CryptoError as exc:
        log.error("smtp_test: cannot decrypt password: %s", exc)
        return web.json_response(
            {"ok": False, "error": f"cannot decrypt stored password: {exc}"},
            status=500,
        )

    message = mailer.build_test_message(from_addr, to_addr)

    try:
        await mailer.send_message(
            message,
            host=host,
            port=int(smtp.get("port") or 587),
            user=smtp.get("user", "") or "",
            password=plaintext,
            tls=smtp.get("tls") or "starttls",
            # Tight timeout so the UI doesn't hang forever if the
            # relay is misconfigured — users want fast feedback
            # from a test button.
            timeout=15.0,
        )
    except mailer.MailerError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc)},
            status=502,
        )

    return web.json_response({
        "ok": True,
        "to": to_addr,
        "from": from_addr,
        "host": host,
    })


async def handle_schedule_run_now(request: web.Request) -> web.Response:
    """POST /api/schedules/{id}/run-now — synchronously render and
    send one specific schedule's report. Used by the Report view's
    "Run now" button.

    Returns the same shape as the SMTP test on success/failure so
    the frontend can share a single result-display component.
    """
    if not _origin_is_same(request):
        return web.json_response({"error": "cross-origin rejected"}, status=403)

    schedule_id = request.match_info.get("id", "")
    if not schedule_id:
        return web.json_response({"error": "schedule id required"}, status=400)

    data = load_settings(SETTINGS_FILE)
    schedules = data.get("schedules") or []
    schedule = next(
        (s for s in schedules if isinstance(s, dict) and s.get("id") == schedule_id),
        None,
    )
    if schedule is None:
        return web.json_response(
            {"ok": False, "error": f"schedule {schedule_id!r} not found"},
            status=404,
        )

    recipients = schedule.get("recipients") or []
    if not recipients:
        return web.json_response(
            {"ok": False, "error": "schedule has no recipients"},
            status=400,
        )

    smtp = data.get("smtp", {})
    host = smtp.get("host") or ""
    if not host:
        return web.json_response(
            {"ok": False, "error": "SMTP host is not configured"},
            status=400,
        )

    try:
        key = crypto.load_or_create_key(SECRET_KEY_FILE)
        plaintext = crypto.decrypt(smtp.get("password_enc", ""), key)
    except crypto.CryptoError as exc:
        return web.json_response(
            {"ok": False, "error": f"cannot decrypt password: {exc}"},
            status=500,
        )

    version = _read_version()
    try:
        message = render.generate_report(
            template=schedule.get("template", "daily"),
            db_file=DB_FILE,
            inventory_file=INVENTORY_FILE,
            settings_file=SETTINGS_FILE,
            version=version,
        )
    except render.RenderError as exc:
        return web.json_response(
            {"ok": False, "error": f"render failed: {exc}"},
            status=500,
        )

    from_addr = smtp.get("from") or smtp.get("user") or "gpu-monitor@localhost"
    message["From"] = from_addr
    message["To"] = ", ".join(recipients)

    try:
        await mailer.send_message(
            message,
            host=host,
            port=int(smtp.get("port") or 587),
            user=smtp.get("user", "") or "",
            password=plaintext,
            tls=smtp.get("tls") or "starttls",
            timeout=30.0,
        )
    except mailer.MailerError as exc:
        return web.json_response(
            {"ok": False, "error": str(exc)},
            status=502,
        )

    # CONCURRENCY: Reload settings right before persisting so we
    # patch ONLY the target schedule's last_run_epoch without
    # clobbering unrelated changes a concurrent writer made while
    # render + send were in flight. Between the earlier load_settings
    # at the top of this handler and now, the user might have PUT
    # an updated smtp.host via the Settings view, or the scheduler
    # subprocess might have fired another schedule and written its
    # own last_run_epoch. Saving our stale `data` snapshot would be
    # a last-write-wins overwrite — the lost-update pattern.
    #
    # Reload-and-patch is the smallest fix: we re-read the latest
    # on-disk state, locate the target schedule by id, stamp its
    # last_run_epoch, and save. This is still not fully transactional
    # (a third writer could race us between the reload and the save),
    # but that would require file-level locking or a compare-and-swap
    # loop which is overkill for a settings file that's mutated
    # at most a few times per minute by hand-operated UIs. The
    # narrow window here is "between reload and save" which is
    # measured in microseconds.
    import time
    now_epoch = int(time.time())
    try:
        latest = load_settings(SETTINGS_FILE)
        latest_schedules = list(latest.get("schedules") or [])
        for s in latest_schedules:
            if isinstance(s, dict) and s.get("id") == schedule_id:
                s["last_run_epoch"] = now_epoch
                break
        latest["schedules"] = latest_schedules
        save_settings(SETTINGS_FILE, latest)
    except OSError as exc:
        log.warning("run_now: could not persist last_run_epoch: %s", exc)

    return web.json_response({
        "ok": True,
        "schedule_id": schedule_id,
        "recipients": recipients,
        "last_run_epoch": now_epoch,
    })


async def handle_report_preview(request: web.Request) -> web.Response:
    """GET /api/reports/preview?template=daily[&theme=dark] — return
    the rendered HTML body of a report (no images, no charts). Used
    by the Report view's <iframe src=...> so the user can see
    roughly what their scheduled email will look like before wiring
    up SMTP.

    include_charts=False mode skips matplotlib entirely so this
    endpoint is cheap to hit repeatedly. The iframe wouldn't
    display cid: references anyway — it's not a MIME client.

    The optional `?theme=dark` query param appends a dark-mode
    CSS override <style> block to the rendered HTML so the
    iframe embed matches the dashboard's dark theme. Recognized
    values: "dark" (explicit dark), anything else (default light).
    The email-send path in POST /api/schedules/{id}/run-now does
    NOT pass this flag — real email recipients always get the
    light template.
    """
    template = request.query.get("template") or "daily"
    # Only "dark" flips the override; any other value (absent,
    # empty, "light", typos) falls back to the light default so
    # the mail-send behavior is never accidentally dark-themed.
    preview_theme = "dark" if request.query.get("theme") == "dark" else "light"

    try:
        message = render.generate_report(
            template=template,
            db_file=DB_FILE,
            inventory_file=INVENTORY_FILE,
            settings_file=SETTINGS_FILE,
            version=_read_version(),
            include_charts=False,
            preview_theme=preview_theme,
        )
    except render.RenderError as exc:
        # SECURITY: The RenderError message can embed the caller-
        # supplied `template` query parameter (e.g. "unknown template
        # 'evilpayload'"). Interpolating that raw into the HTML
        # response would be a reflected-XSS vector — a crafted
        # ?template=<script>alert(1)</script> would end up as
        # executable script in the returned page. html.escape()
        # converts the five XSS-relevant ASCII characters (<, >,
        # &, ", ') to their entity form, which the browser renders
        # as literal text. The parent <iframe sandbox> in the Report
        # view provides defense-in-depth, but the fix belongs here
        # at the boundary where untrusted content becomes HTML.
        import html as html_module
        safe_message = html_module.escape(str(exc))
        return web.Response(
            text=(
                "<html><body>"
                "<h1>Preview failed</h1>"
                f"<p>{safe_message}</p>"
                "</body></html>"
            ),
            status=400,
            content_type="text/html",
        )

    # Extract the HTML body from the multipart/alternative message.
    # include_charts=False means there's no multipart/related
    # wrapper — the text/html is a direct subpart.
    html_body = ""
    for part in message.walk():
        if part.get_content_type() == "text/html":
            html_body = part.get_content()
            break

    return web.Response(
        text=html_body or "<html><body>No preview available.</body></html>",
        content_type="text/html",
        charset="utf-8",
    )


# ─── Housekeeping (Phase 6.2) ───────────────────────────────────────────────


async def handle_db_info(request: web.Request) -> web.Response:
    """Return a snapshot of the metrics database's physical state:
    file size, total row count, oldest/newest sample epoch, and per-GPU
    row counts. Used by the Settings → Housekeeping tab to show the
    user what the container is currently storing.

    The file size includes the main .db file only — the WAL / SHM
    sidecar files from Phase 1's WAL mode are transient and their
    size depends on recent write activity, so including them would
    make the reported size flap.
    """
    try:
        size_bytes = DB_FILE.stat().st_size if DB_FILE.exists() else 0
    except OSError as exc:
        log.warning("db_info: stat failed: %s", exc)
        size_bytes = 0

    try:
        conn = _open_db_readonly()
    except sqlite3.OperationalError as exc:
        log.warning("db_info: cannot open DB: %s", exc)
        return web.json_response({
            "size_bytes": size_bytes,
            "row_count": 0,
            "oldest_epoch": None,
            "newest_epoch": None,
            "row_count_per_gpu": [],
        })

    try:
        try:
            summary = conn.execute("""
                SELECT
                    COUNT(*)             AS row_count,
                    MIN(timestamp_epoch) AS oldest,
                    MAX(timestamp_epoch) AS newest
                FROM gpu_metrics
            """).fetchone()
            per_gpu_rows = conn.execute("""
                SELECT gpu_index, COUNT(*) AS n
                FROM gpu_metrics
                GROUP BY gpu_index
                ORDER BY gpu_index ASC
            """).fetchall()
        except sqlite3.OperationalError as exc:
            log.warning("db_info: query failed: %s", exc)
            return web.json_response({
                "size_bytes": size_bytes,
                "row_count": 0,
                "oldest_epoch": None,
                "newest_epoch": None,
                "row_count_per_gpu": [],
            })
    finally:
        conn.close()

    return web.json_response({
        "size_bytes": size_bytes,
        "row_count": int(summary["row_count"] or 0),
        "oldest_epoch": summary["oldest"],
        "newest_epoch": summary["newest"],
        "row_count_per_gpu": [
            {"gpu_index": r["gpu_index"], "row_count": int(r["n"])}
            for r in per_gpu_rows
        ],
    })


async def handle_vacuum(request: web.Request) -> web.Response:
    """Run SQLite VACUUM on the metrics database and return the freed
    bytes. VACUUM rebuilds the file from scratch, compacting free
    pages that accumulated from DELETE operations (notably
    clean_old_data running nightly).

    VACUUM requires the database to not be held in any transaction by
    another connection, and holds an exclusive write lock for the
    duration. On a homelab-sized DB (~100 MB) this takes a few
    seconds; on a 1 GB+ DB it can take a minute or more. The 30 s
    connection timeout above caps the worst case — if we can't
    acquire the lock in 30 s we return 503 rather than hanging the
    request forever.
    """
    if not _origin_is_same(request):
        return web.json_response({"error": "cross-origin rejected"}, status=403)

    try:
        before = DB_FILE.stat().st_size if DB_FILE.exists() else 0
    except OSError:
        before = 0

    try:
        conn = _open_db_readwrite()
    except sqlite3.OperationalError as exc:
        log.warning("vacuum: cannot open DB for write: %s", exc)
        return web.json_response(
            {"error": "database unavailable", "detail": str(exc)},
            status=503,
        )

    try:
        try:
            # VACUUM cannot run inside a transaction. isolation_level=None
            # disables the implicit BEGIN sqlite3 normally wraps around
            # executes — essential for VACUUM, harmless for everything
            # else we might run here.
            conn.isolation_level = None
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            log.warning("vacuum: execute failed: %s", exc)
            return web.json_response(
                {"error": "vacuum failed", "detail": str(exc)},
                status=503,
            )
    finally:
        conn.close()

    try:
        after = DB_FILE.stat().st_size if DB_FILE.exists() else before
    except OSError:
        after = before

    # freed_bytes can be negative if VACUUM actually grew the file
    # (uncommon but possible if fragmentation was minimal and the
    # rebuilt version pads). Report the raw delta; the UI shows "0 MB
    # freed" for non-positive deltas.
    freed = before - after
    return web.json_response({
        "ok": True,
        "size_before": before,
        "size_after": after,
        "freed_bytes": freed,
    })


async def handle_purge(request: web.Request) -> web.Response:
    """Delete rows older than N days. Body: {"days": N} where
    N is a positive integer. Returns the number of rows deleted.

    This is the user-triggered version of the collector's nightly
    clean_old_data, for cases where someone wants to manually reclaim
    space without waiting for the daily sweep. Always safe: the
    DELETE is bounded by `timestamp_epoch < (now - N days)`, so the
    same request run twice is idempotent (the second run deletes
    zero rows).
    """
    if not _origin_is_same(request):
        return web.json_response({"error": "cross-origin rejected"}, status=403)

    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)

    days_raw = body.get("days")
    if not isinstance(days_raw, int) or isinstance(days_raw, bool):
        return web.json_response(
            {"error": "days must be a positive integer"}, status=400
        )
    # Upper bound: 365 (one year). Lower bound: 1 (purging with days=0
    # would wipe everything, which the UI should never do — manual
    # factory-reset is a different operation).
    if days_raw < 1 or days_raw > 365:
        return web.json_response(
            {"error": "days must be between 1 and 365"}, status=400
        )

    cutoff_seconds = days_raw * 86400

    try:
        conn = _open_db_readwrite()
    except sqlite3.OperationalError as exc:
        log.warning("purge: cannot open DB for write: %s", exc)
        return web.json_response(
            {"error": "database unavailable", "detail": str(exc)},
            status=503,
        )

    try:
        try:
            cursor = conn.execute(
                """
                DELETE FROM gpu_metrics
                WHERE timestamp_epoch < (strftime('%s', 'now') - ?)
                """,
                (cutoff_seconds,),
            )
            rows_deleted = cursor.rowcount
            conn.commit()
        except sqlite3.OperationalError as exc:
            log.warning("purge: execute failed: %s", exc)
            return web.json_response(
                {"error": "purge failed", "detail": str(exc)},
                status=503,
            )
    finally:
        conn.close()

    return web.json_response({
        "ok": True,
        "days": days_raw,
        "rows_deleted": int(rows_deleted or 0),
    })


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

    # Phase 6b — scheduled reports
    app.router.add_post("/api/schedules/{id}/run-now", handle_schedule_run_now)
    app.router.add_get ("/api/reports/preview",        handle_report_preview)

    # Phase 6.2 — housekeeping. Read-only db-info is unauthenticated;
    # vacuum/purge enforce same-origin because they mutate data.
    app.router.add_get ("/api/housekeeping/db-info", handle_db_info)
    app.router.add_post("/api/housekeeping/vacuum",  handle_vacuum)
    app.router.add_post("/api/housekeeping/purge",   handle_purge)

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
    log.info("       /api/schedules/{id}/run-now, /api/reports/preview")
    log.info("       /api/housekeeping/{db-info, vacuum, purge}")
    log.info("========================================")
    web.run_app(make_app(), port=8081, access_log=None)
