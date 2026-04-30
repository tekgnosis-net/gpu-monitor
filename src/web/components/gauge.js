/*
 * <gpu-gauge> — Horizontal progress-bar gauge for a metric.
 *
 * Phase 4. Replaces the div-based gauges in the legacy HTML with a
 * proper web component. Takes a value, a max, a label, and a unit
 * string; draws a filled bar whose color adapts to the fill percentage
 * (green under 50%, orange 50–80%, red above 80%).
 *
 * Usage:
 *   <gpu-gauge
 *       metric="temperature"
 *       label="Temperature"
 *       value="58"
 *       max="100"
 *       unit="°C">
 *   </gpu-gauge>
 *
 * Dynamic max values (e.g. GPU-specific memory_total_mib from
 * /api/gpus) replace the hardcoded 24576 and 250 ceilings the
 * pre-Phase-4 legacy HTML had baked in.
 */

// jsDelivr's `+esm` flag goes directly after the package name (not
// after a file path). An earlier version had `lit@3/index.js+esm`
// which jsDelivr interprets as a literal path and serves 404,
// silently breaking every Lit component in the browser. Pinned to
// 3.2.1 for reproducibility — v1.0.0 test suites are server-side
// pytest only and would not have caught a URL format bug before
// it reached a real browser.
import { LitElement, html, css } from 'https://cdn.jsdelivr.net/npm/lit@3.2.1/+esm';

class GpuGauge extends LitElement {
    static properties = {
        metric: { type: String },
        label:  { type: String },
        value:  { type: Number },
        max:    { type: Number },
        unit:   { type: String },
    };

    constructor() {
        super();
        this.metric = '';
        this.label = '';
        this.value = 0;
        this.max = 100;
        this.unit = '';
    }

    static styles = css`
        :host {
            display: block;
            padding: 12px 0;
            border-bottom: 1px solid var(--border-subtle, rgba(0, 0, 0, 0.08));
        }

        :host(:last-of-type) {
            border-bottom: none;
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 8px;
        }

        .label {
            font-size: var(--font-size-sm, 13px);
            color: var(--text-tertiary, #666);
            font-weight: 500;
        }

        /* In-line "(96.4%)" suffix on memory/power labels. Tabular
         * numerals stop the digits from jiggling horizontally as
         * the value updates every poll cycle. The lighter weight
         * keeps the percentage visually subordinate to the metric
         * name itself, even though both share the tertiary color. */
        .label-pct {
            font-weight: 400;
            margin-left: 4px;
            font-variant-numeric: tabular-nums;
        }

        .value {
            font-size: var(--font-size-lg, 17px);
            font-weight: 600;
            font-variant-numeric: tabular-nums;
            color: var(--text-primary, #000);
        }

        .value .unit {
            font-size: var(--font-size-sm, 13px);
            font-weight: 400;
            color: var(--text-tertiary, #666);
            margin-left: 4px;
        }

        .track {
            position: relative;
            height: 8px;
            background: var(--bg-tertiary, rgba(0, 0, 0, 0.05));
            border-radius: 4px;
            overflow: hidden;
        }

        .fill {
            position: absolute;
            top: 0;
            left: 0;
            bottom: 0;
            border-radius: 4px;
            background: var(--success, #34c759);
            /* Use the shared motion token so the transition collapses
             * to 0ms for users with prefers-reduced-motion. The token
             * packs duration + easing, so the shorthand expands to a
             * complete transition declaration. */
            transition:
                width var(--motion-normal, 250ms cubic-bezier(0.4, 0, 0.2, 1)),
                background var(--motion-normal, 250ms cubic-bezier(0.4, 0, 0.2, 1));
        }

        .fill.warn {
            background: var(--warning, #ff9500);
        }

        .fill.danger {
            background: var(--danger, #ff3b30);
        }
    `;

    _pct() {
        if (!this.max || this.max <= 0) return 0;
        const pct = (Number(this.value) / Number(this.max)) * 100;
        if (!isFinite(pct) || pct < 0) return 0;
        if (pct > 100) return 100;
        return pct;
    }

    // Phase 7 / task #29: Sanitize the value before it hits
    // aria-valuenow so screen readers never announce NaN, Infinity,
    // or unclamped negatives / over-maxes. The visible display uses
    // _displayValue() which has its own NaN → "—" handling; AT
    // users need a finite number inside the ARIA progressbar
    // contract instead. The sanitized value is clamped into
    // [0, max] so assistive tech that computes percent internally
    // (e.g. VoiceOver) produces matching output to the visible bar.
    _sanitizedValue() {
        const v = Number(this.value);
        if (!isFinite(v)) return 0;
        const max = Number(this.max);
        const upperBound = isFinite(max) && max > 0 ? max : 100;
        if (v < 0) return 0;
        if (v > upperBound) return upperBound;
        return v;
    }

    _sanitizedMax() {
        const max = Number(this.max);
        if (!isFinite(max) || max <= 0) return 100;
        return max;
    }

    _colorClass(pct) {
        if (pct >= 80) return 'danger';
        if (pct >= 50) return 'warn';
        return '';
    }

    _displayValue() {
        const v = Number(this.value);
        if (!isFinite(v)) return '—';
        // Compact formatting: drop trailing .0, round floats to 1 decimal
        if (Number.isInteger(v)) return String(v);
        return v.toFixed(1);
    }

    // Hover tooltip: shows the absolute value and the percentage-of-max
    // in brackets. Anchors the bracket to the visible max so hovering
    // a Memory bar at 50% on a 24 GiB card produces "12288 MiB
    // (50.0% of 24576 MiB)" — meaningful — rather than just "50%"
    // which is what aria-valuenow already announces.
    //
    // Skip the bracket entirely when unit === '%' (utilization) because
    // the value IS already a percentage and "73 % (73.0%)" would be
    // redundant. Skip the entire tooltip when value is non-finite so
    // hovering an unavailable metric shows "unavailable" instead of
    // "—  (NaN%)".
    _tooltip() {
        const v = Number(this.value);
        if (!isFinite(v)) {
            return `${this.label}: unavailable`;
        }
        const unitSuffix = this.unit ? ` ${this.unit}` : '';
        const valueText = `${this._displayValue()}${unitSuffix}`;
        if (this.unit === '%') {
            return `${this.label}: ${valueText}`;
        }
        const max = this._sanitizedMax();
        const pct = this._pct().toFixed(1);
        return `${this.label}: ${valueText} (${pct}% of ${max}${unitSuffix})`;
    }

    // Memory and Power gauges show the percentage-of-cap inline with
    // the label — "Memory (96.4%) ... 23700 MiB" — so users can read
    // utilization at a glance without having to hover for the tooltip.
    //
    // Skip for:
    //   * temperature (the "100 °C" max is an arbitrary display
    //     ceiling, not a meaningful denominator like memory_total
    //     or power_limit_w),
    //   * utilization (value is already a percentage; "Utilization
    //     (73%) ... 73 %" would be redundant noise),
    //   * non-finite values (would render as "(NaN%)").
    _shouldShowLabelPct() {
        if (this.metric !== 'memory' && this.metric !== 'power') return false;
        if (this.unit === '%') return false;
        return isFinite(Number(this.value));
    }

    render() {
        const pct = this._pct();
        const colorClass = this._colorClass(pct);
        const tooltip = this._tooltip();
        const showLabelPct = this._shouldShowLabelPct();
        return html`
            <div class="header">
                <span class="label">
                    ${this.label}${showLabelPct
                        ? html`<span class="label-pct">(${pct.toFixed(1)}%)</span>`
                        : ''}
                </span>
                <span class="value">
                    ${this._displayValue()}<span class="unit">${this.unit}</span>
                </span>
            </div>
            <div class="track" role="progressbar"
                 title=${tooltip}
                 aria-label=${this.label}
                 aria-valuenow=${this._sanitizedValue()}
                 aria-valuemin="0"
                 aria-valuemax=${this._sanitizedMax()}>
                <div class="fill ${colorClass}" style="width: ${pct}%"></div>
            </div>
        `;
    }
}

customElements.define('gpu-gauge', GpuGauge);
