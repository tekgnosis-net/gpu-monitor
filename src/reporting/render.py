"""
HTML email report rendering with embedded PNG charts.

Phase 6b of the v1.0.0 overhaul. Produces a multipart/related
EmailMessage suitable for handing to reporting.mailer.send_message.
The structure is:

    multipart/alternative
    ├── text/plain           (minimal fallback body)
    └── multipart/related    (HTML + embedded images)
        ├── text/html        (inlined-CSS via premailer)
        ├── image/png        (Content-ID: chart_temp_gpu0)
        ├── image/png        (Content-ID: chart_power_gpu0)
        └── ...

Why multipart/related inside multipart/alternative?

  Most mail clients walk multipart/alternative and pick the
  richest subpart they can render. If we put the text/plain at
  the top level and the HTML inside multipart/related, Outlook
  and Gmail show the HTML correctly. Putting just text/html at
  the top level with embedded cid: images works in Gmail but
  breaks in Outlook/Apple Mail which expect a multipart/related
  container for "HTML with resources".

Chart generation

  Uses matplotlib's 'Agg' backend (no display required) and a
  hand-tuned Apple-HIG-ish palette. Each chart is rendered at
  2× DPI (160 dpi) for retina displays, then serialized to PNG
  via BytesIO and attached as a MIMEImage with a unique
  Content-ID matching the template's cid: references.

  Per GPU we produce two charts: temperature trend and power
  trend. Utilization and memory are summarized numerically in
  the template but don't get their own chart — four charts per
  GPU gets noisy on multi-GPU setups and blows up the email
  size budget.

Settings dependency

  The render module reads electricity rate + currency from
  settings.json (via reporting.settings.load_settings) to
  compute the "Estimated cost" aggregate tile. The cost path
  only activates when rate_per_kwh > 0; at rate=0 (default)
  the tile shows the literal "$0.00" placeholder. No Phase 6a
  / 6b routing dependency — render.py reads from the same
  settings file the server mutates.

Database dependency

  Reads raw samples from history/gpu_metrics.db via a read-only
  SQLite connection. The same WAL mode that lets the API
  endpoints coexist with the collector applies here: render can
  read while the collector is writing, no locks.
"""

from __future__ import annotations

import io
import logging
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from uuid import uuid4

import jinja2

# Matplotlib MUST pick the Agg backend before pyplot is imported —
# otherwise it'll try to initialize a GUI backend and fail in the
# headless container. `use('Agg', force=True)` is the idiomatic
# escape hatch.
import matplotlib
matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt  # noqa: E402

import premailer  # noqa: E402

from reporting.settings import load_settings  # noqa: E402


log = logging.getLogger("gpu-monitor.render")


class RenderError(Exception):
    """Raised when the report cannot be rendered. Distinct from
    library exceptions so the API handlers see a single type."""


# ─── Constants ──────────────────────────────────────────────────────────────

# Apple HIG-inspired chart colors. Match the frontend's dashboard
# palette so an emailed chart "feels" visually continuous with the
# live view. Hex values lifted from tokens.css.
COLOR_TEMP = "#ff3b30"    # system red
COLOR_POWER = "#af52de"   # system purple
COLOR_GRID = "#e5e5ea"    # border-subtle equivalent
COLOR_TEXT = "#1d1d1f"
COLOR_TEXT_MUTED = "#6e6e73"

# Figure size in inches × DPI. 7.5×3.0 inches × 160 dpi = 1200×480 px
# → retina-crisp on phone + desktop, ~50 KB per PNG at reasonable
# line complexity. The plan noted the image-size cost is accepted.
FIG_WIDTH_IN = 7.5
FIG_HEIGHT_IN = 3.0
FIG_DPI = 160

# Window presets that match the scheduler's template enum. The
# scheduler fires with template="daily" / "weekly" / "monthly"; this
# table translates that to a lookback window for the DB query and a
# human label for the template.
RANGE_BY_TEMPLATE = {
    "daily":   (24 * 3600,      "last 24 hours"),
    "weekly":  (7 * 86400,      "last 7 days"),
    "monthly": (30 * 86400,     "last 30 days"),
}


# ─── Jinja environment ─────────────────────────────────────────────────────

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_jinja_env: jinja2.Environment | None = None


def _get_jinja() -> jinja2.Environment:
    """Lazy-initialize the Jinja environment so tests that don't
    render a full report don't pay the startup cost."""
    global _jinja_env
    if _jinja_env is None:
        _jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=jinja2.select_autoescape(["html", "j2"]),
            # trim_blocks + lstrip_blocks produce tighter HTML
            # without leading whitespace from the `{% %}` tags.
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _jinja_env


# ─── Chart rendering ───────────────────────────────────────────────────────


def _render_line_chart(
    x_labels: list[str],
    y_values: list[float],
    *,
    title: str,
    y_label: str,
    color: str,
) -> bytes:
    """Render a single-series line chart to PNG bytes.

    x_labels: ISO-format timestamps as strings (from the DB row's
    `timestamp` text column). Not parsed — matplotlib treats them
    as categorical x ticks, which matches what Chart.js does on the
    frontend and keeps the render path fast.

    Returns the raw PNG bytes. Does NOT attach to any message — the
    caller wraps these in MIMEImage with a fresh Content-ID.
    """
    if not x_labels or not y_values:
        # Empty chart — matplotlib can't plot zero points, so we
        # return a small placeholder image with "No data" text.
        return _render_empty_chart(title, y_label)

    fig, ax = plt.subplots(figsize=(FIG_WIDTH_IN, FIG_HEIGHT_IN), dpi=FIG_DPI)

    # Position data on integer indices — matches Chart.js categorical
    # x-axis behavior and avoids parsing timestamp strings.
    xs = list(range(len(x_labels)))
    ax.plot(xs, y_values, color=color, linewidth=2.0)
    ax.fill_between(xs, y_values, alpha=0.12, color=color)

    # Aesthetic: HIG-ish minimal axis chrome
    ax.set_title(title, loc="left", color=COLOR_TEXT,
                 fontsize=12, fontweight="semibold", pad=10)
    ax.set_ylabel(y_label, color=COLOR_TEXT_MUTED, fontsize=10)
    ax.tick_params(colors=COLOR_TEXT_MUTED, labelsize=9)
    ax.grid(True, color=COLOR_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(COLOR_GRID)

    # Only show ~8 x-tick labels so dense data doesn't turn the
    # axis into a black bar of timestamps. Pick evenly-spaced
    # indices and label them with the corresponding x_label entry.
    if len(x_labels) > 8:
        tick_count = 8
        step = max(1, len(x_labels) // (tick_count - 1))
        tick_indices = list(range(0, len(x_labels), step))
        if tick_indices[-1] != len(x_labels) - 1:
            tick_indices.append(len(x_labels) - 1)
        ax.set_xticks(tick_indices)
        ax.set_xticklabels(
            [x_labels[i][-8:] for i in tick_indices],  # HH:MM:SS
            rotation=0,
        )
    else:
        ax.set_xticks(xs)
        ax.set_xticklabels(
            [label[-8:] for label in x_labels],
            rotation=0,
        )

    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=FIG_DPI,
                facecolor="white", edgecolor="none")
    plt.close(fig)  # free the figure memory — MATPLOTLIB WILL LEAK WITHOUT THIS
    buf.seek(0)
    return buf.getvalue()


def _render_empty_chart(title: str, y_label: str) -> bytes:
    """Render a 'no data' placeholder image at the same dimensions
    as a real chart so the email layout doesn't jump when a GPU has
    an empty window."""
    fig, ax = plt.subplots(figsize=(FIG_WIDTH_IN, FIG_HEIGHT_IN), dpi=FIG_DPI)
    ax.text(
        0.5, 0.5, "No data in this window",
        horizontalalignment="center", verticalalignment="center",
        transform=ax.transAxes, color=COLOR_TEXT_MUTED, fontsize=13,
    )
    ax.set_title(title, loc="left", color=COLOR_TEXT,
                 fontsize=12, fontweight="semibold", pad=10)
    ax.set_ylabel(y_label, color=COLOR_TEXT_MUTED, fontsize=10)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=FIG_DPI, facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


# ─── DB summarization ──────────────────────────────────────────────────────


def _summarize_gpu(conn: sqlite3.Connection, gpu_index: int, window_seconds: int) -> dict:
    """Fetch per-GPU aggregates + raw series for charting.

    Two queries per GPU:
      1. SUM/MIN/MAX/AVG aggregation for the stat rows (reuses the
         same CASE WHEN power > 0 guard as handle_stats_power)
      2. Raw (timestamp, temperature, power) rows for chart series

    Both queries are served from the Phase 1 (gpu_index,
    timestamp_epoch) composite index. Cheap even on 30-day windows.
    """
    stats = conn.execute(
        """
        SELECT
            COUNT(*) AS n_total,
            MIN(temperature) AS temp_min,
            AVG(temperature) AS temp_avg,
            MAX(temperature) AS temp_max,
            AVG(utilization) AS util_avg,
            MAX(utilization) AS util_max,
            COALESCE(
                SUM(CASE WHEN power > 0 THEN power * interval_s ELSE 0 END),
                0
            ) / 3600.0 AS energy_wh,
            COALESCE(MAX(CASE WHEN power > 0 THEN power END), 0) AS peak_w,
            COALESCE(AVG(CASE WHEN power > 0 THEN power END), 0) AS avg_w
        FROM gpu_metrics
        WHERE gpu_index = ?
          AND timestamp_epoch > strftime('%s', 'now') - ?
        """,
        (gpu_index, window_seconds),
    ).fetchone()

    rows = conn.execute(
        """
        SELECT timestamp, temperature, power
        FROM gpu_metrics
        WHERE gpu_index = ?
          AND timestamp_epoch > strftime('%s', 'now') - ?
        ORDER BY timestamp_epoch ASC
        """,
        (gpu_index, window_seconds),
    ).fetchall()

    return {
        "n_total": int(stats["n_total"] or 0),
        "temp_min": round(float(stats["temp_min"] or 0), 1),
        "temp_avg": round(float(stats["temp_avg"] or 0), 1),
        "temp_max": round(float(stats["temp_max"] or 0), 1),
        "util_avg": round(float(stats["util_avg"] or 0), 1),
        "util_max": round(float(stats["util_max"] or 0), 1),
        "energy_wh": float(stats["energy_wh"] or 0),
        "peak_w": float(stats["peak_w"] or 0),
        "avg_w": float(stats["avg_w"] or 0),
        "timestamps": [r["timestamp"] for r in rows],
        "temperatures": [float(r["temperature"]) for r in rows],
        "powers": [float(r["power"]) for r in rows if r["power"] is not None],
        "power_timestamps": [
            r["timestamp"] for r in rows if r["power"] is not None
        ],
    }


def _load_inventory(inventory_file: Path) -> list[dict]:
    """Read the GPU inventory written by discover_gpus at container
    startup. Returns [] on any read error; the caller's empty-state
    path takes over."""
    try:
        import json
        data = json.loads(inventory_file.read_text())
        return list(data.get("gpus", []))
    except (OSError, ValueError):
        return []


# ─── Format helpers ────────────────────────────────────────────────────────


def _format_energy(wh: float) -> tuple[str, str]:
    """Convert Wh to a (value_str, unit_str) pair. <1000 Wh shows as
    Wh; >=1000 shows as kWh with 2 decimals."""
    if wh < 1000:
        return (f"{wh:.0f}", "Wh")
    return (f"{wh / 1000:.2f}", "kWh")


def _format_cost(wh: float, rate_per_kwh: float, currency: str) -> str:
    """kWh × rate → currency-prefixed string. Always 2 decimals."""
    cost = (wh / 1000.0) * rate_per_kwh
    return f"{currency}{cost:.2f}"


# ─── Main entrypoint ───────────────────────────────────────────────────────


def generate_report(
    *,
    template: str,
    db_file: Path,
    inventory_file: Path,
    settings_file: Path,
    version: str,
    include_charts: bool = True,
) -> EmailMessage:
    """Build a full multipart/alternative EmailMessage for the given
    template ("daily"/"weekly"/"monthly"). Caller sets the Subject/
    From/To headers on the returned message before passing to
    reporting.mailer.send_message — the render step doesn't know
    the recipient.

    The EmailMessage returned is ALMOST ready to send:
      * Subject is pre-set to a human-readable string
      * multipart/alternative with text/plain fallback
      * multipart/related inside with HTML + CID-attached PNG charts
      * From/To: NOT set (caller's job)

    include_charts=False renders the template without any <img>
    references and skips the matplotlib work entirely — useful for
    the GET /api/reports/preview endpoint which streams the HTML
    straight to an <iframe srcdoc> without needing to round-trip
    images through a server-side CID resolver.
    """
    if template not in RANGE_BY_TEMPLATE:
        raise RenderError(f"unknown template {template!r}")
    window_seconds, range_label = RANGE_BY_TEMPLATE[template]

    settings_data = load_settings(settings_file)
    power_cfg = settings_data.get("power", {})
    rate_per_kwh = float(power_cfg.get("rate_per_kwh") or 0)
    currency = str(power_cfg.get("currency") or "$")

    inventory = _load_inventory(inventory_file)
    if not inventory:
        # Empty inventory → still render, just with a single "no
        # GPUs detected" card. Prevents the scheduler from silently
        # no-op'ing when nvidia-smi isn't available.
        inventory = [{"index": 0, "name": "(no GPUs detected)"}]

    # Fetch per-GPU aggregates + series via a single read-only DB
    # connection. Shared across all GPUs' queries to amortize the
    # connect cost.
    try:
        uri = f"file:{db_file}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError as exc:
        raise RenderError(f"cannot open metrics database: {exc}") from exc

    chart_attachments: list[tuple[str, bytes]] = []
    gpus_for_template: list[dict] = []

    try:
        for gpu in sorted(inventory, key=lambda g: int(g.get("index") or 0)):
            gpu_index = int(gpu.get("index") or 0)
            name = gpu.get("name") or f"GPU {gpu_index}"

            try:
                summary = _summarize_gpu(conn, gpu_index, window_seconds)
            except sqlite3.OperationalError as exc:
                log.warning("render: summarize failed for gpu %d: %s",
                            gpu_index, exc)
                summary = {
                    "n_total": 0, "temp_min": 0, "temp_avg": 0, "temp_max": 0,
                    "util_avg": 0, "util_max": 0, "energy_wh": 0,
                    "peak_w": 0, "avg_w": 0,
                    "timestamps": [], "temperatures": [],
                    "powers": [], "power_timestamps": [],
                }

            energy_value, energy_unit = _format_energy(summary["energy_wh"])

            gpu_ctx: dict[str, Any] = {
                "index": gpu_index,
                "name": name,
                "samples": summary["n_total"],
                "temp_min": summary["temp_min"],
                "temp_avg": summary["temp_avg"],
                "temp_max": summary["temp_max"],
                "util_avg": summary["util_avg"],
                "util_max": summary["util_max"],
                "energy_value": energy_value,
                "energy_unit": energy_unit,
                "peak_w": summary["peak_w"],
                "avg_w": summary["avg_w"],
                "chart_cids": None,
            }

            if include_charts:
                temp_cid  = f"chart_temp_gpu{gpu_index}_{uuid4().hex[:8]}"
                power_cid = f"chart_power_gpu{gpu_index}_{uuid4().hex[:8]}"

                temp_png = _render_line_chart(
                    summary["timestamps"], summary["temperatures"],
                    title=f"{name} — temperature",
                    y_label="°C", color=COLOR_TEMP,
                )
                power_png = _render_line_chart(
                    summary["power_timestamps"], summary["powers"],
                    title=f"{name} — power",
                    y_label="W", color=COLOR_POWER,
                )
                chart_attachments.append((temp_cid, temp_png))
                chart_attachments.append((power_cid, power_png))
                gpu_ctx["chart_cids"] = {
                    "temp": temp_cid,
                    "power": power_cid,
                }

            gpus_for_template.append(gpu_ctx)
    finally:
        conn.close()

    # Aggregate totals across all GPUs for the header tile row
    total_energy = sum(g["energy_value_float"] for g in _with_float_energy(gpus_for_template))
    total_peak = max((g["peak_w"] for g in gpus_for_template), default=0.0)
    agg_energy_value, agg_energy_unit = _format_energy(total_energy)
    aggregate_ctx = {
        "energy_value": agg_energy_value,
        "energy_unit": agg_energy_unit,
        "peak_w": total_peak,
        "cost_display": _format_cost(total_energy, rate_per_kwh, currency),
    }

    # Render the HTML
    jenv = _get_jinja()
    template_obj = jenv.get_template("daily_report.html.j2")
    html = template_obj.render(
        report_title=f"GPU Monitor — {template} report",
        range_label=range_label,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        gpus=gpus_for_template,
        aggregate=aggregate_ctx,
        version=version,
    )

    # Inline the CSS so Gmail/Outlook render it correctly. Premailer
    # warns on any unresolvable selector; `disable_validation=True`
    # silences those (they'd pollute the collector's log without
    # telling us anything actionable).
    try:
        html_inlined = premailer.transform(
            html,
            disable_validation=True,
            disable_leftover_css=True,
            remove_unset_properties=True,
        )
    except Exception as exc:
        # Premailer failures should not kill the report — fall back
        # to the non-inlined HTML. Worst case Gmail ignores the
        # <style> block and the email looks plain.
        log.warning("render: premailer failed, using non-inlined HTML: %s", exc)
        html_inlined = html

    # Plain-text fallback for clients that can't render HTML. Keep it
    # minimal — it's a pure fallback, not a second rendering path.
    plain_lines = [
        f"GPU Monitor — {template} report",
        f"{range_label}",
        "",
    ]
    for gpu in gpus_for_template:
        plain_lines.append(
            f"{gpu['name']} (GPU {gpu['index']}): "
            f"temp {gpu['temp_min']}-{gpu['temp_max']} °C, "
            f"peak {gpu['peak_w']:.1f} W, "
            f"energy {gpu['energy_value']} {gpu['energy_unit']}"
        )
    plain_lines.append("")
    plain_lines.append(f"Sent by GPU Monitor v{version}")
    plain_text = "\n".join(plain_lines)

    # Assemble: multipart/alternative with a plain-text primary + an
    # html alternative that itself carries the inline images as
    # multipart/related content.
    msg = EmailMessage()
    msg["Subject"] = f"GPU Monitor {template} report"
    msg.set_content(plain_text)
    msg.add_alternative(html_inlined, subtype="html")

    if include_charts and chart_attachments:
        # add_related() attaches each PNG as a multipart/related subpart
        # of the HTML alternative, with a Content-ID matching the cid: URL.
        html_part = msg.get_payload()[-1]
        for cid, png_bytes in chart_attachments:
            html_part.add_related(
                png_bytes,
                maintype="image",
                subtype="png",
                cid=f"<{cid}>",
            )

    return msg


def _with_float_energy(gpus: list[dict]) -> list[dict]:
    """Augment each gpu dict with an `energy_value_float` field that
    sums correctly across GPUs regardless of whether the display
    formatted it as Wh or kWh. Used only by generate_report's
    aggregate calculation; kept here rather than inline so the
    intent is explicit (we can't just sum the display strings)."""
    out = []
    for gpu in gpus:
        # The display value was formatted by _format_energy; we need
        # the underlying Wh value for summation. Recompute from the
        # raw fields the GPU context already carries.
        if gpu["energy_unit"] == "kWh":
            wh = float(gpu["energy_value"]) * 1000.0
        else:
            wh = float(gpu["energy_value"])
        out.append({**gpu, "energy_value_float": wh})
    return out
