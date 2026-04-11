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

import { LitElement, html, css } from 'https://cdn.jsdelivr.net/npm/lit@3/index.js+esm';

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

    render() {
        const pct = this._pct();
        const colorClass = this._colorClass(pct);
        return html`
            <div class="header">
                <span class="label">${this.label}</span>
                <span class="value">
                    ${this._displayValue()}<span class="unit">${this.unit}</span>
                </span>
            </div>
            <div class="track" role="progressbar"
                 aria-label=${this.label}
                 aria-valuenow=${this.value}
                 aria-valuemin="0"
                 aria-valuemax=${this.max}>
                <div class="fill ${colorClass}" style="width: ${pct}%"></div>
            </div>
        `;
    }
}

customElements.define('gpu-gauge', GpuGauge);
