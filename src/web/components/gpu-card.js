/*
 * <gpu-card> — Per-GPU metrics card containing four <gpu-gauge>s.
 *
 * Phase 4. A card that shows the current temperature, utilization,
 * memory, and power for a single GPU, driven by dynamic ceilings from
 * the /api/gpus inventory (so we don't hardcode 24576 MiB or 250 W
 * the way the legacy HTML did).
 *
 * Properties:
 *   gpuIndex       — the index reported by nvidia-smi (0, 1, ...)
 *   gpuName        — display name
 *   memoryTotalMib — total memory for the gauge max
 *   powerLimitW    — power limit for the gauge max (may be 0 if
 *                    nvidia-smi reported [N/A])
 *   metrics        — { temperature, utilization, memory, power }
 *                    from /api/metrics/current
 *
 * Because Lit re-renders on property change, feeding this component a
 * new `metrics` object every ~interval_seconds produces smooth updates
 * to all four gauges without any imperative DOM manipulation.
 */

// See gauge.js for the jsDelivr +esm suffix rationale. Same fix
// applied here — URL was 404ing the whole gpu-card component.
import { LitElement, html, css } from 'https://cdn.jsdelivr.net/npm/lit@3.2.1/+esm';

import './gauge.js';

class GpuCard extends LitElement {
    static properties = {
        gpuIndex:       { type: Number, attribute: 'gpu-index' },
        gpuName:        { type: String, attribute: 'gpu-name' },
        memoryTotalMib: { type: Number, attribute: 'memory-total-mib' },
        powerLimitW:    { type: Number, attribute: 'power-limit-w' },
        metrics:        { type: Object },
    };

    constructor() {
        super();
        this.gpuIndex = 0;
        this.gpuName = 'GPU';
        this.memoryTotalMib = 24576;  // sensible fallback for missing inventory
        this.powerLimitW = 300;       // sensible fallback for [N/A] power limits
        this.metrics = {
            temperature: 0,
            utilization: 0,
            memory: 0,
            power: 0,
        };
    }

    static styles = css`
        :host {
            display: block;
            background: var(--bg-secondary, #fff);
            border: 1px solid var(--border-subtle, rgba(0, 0, 0, 0.08));
            border-radius: var(--radius-lg, 14px);
            padding: var(--space-5, 24px);
            box-shadow: var(--shadow-sm, 0 1px 2px rgba(0, 0, 0, 0.05));
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: var(--space-4, 16px);
            padding-bottom: var(--space-3, 12px);
            border-bottom: 1px solid var(--border-subtle, rgba(0, 0, 0, 0.08));
        }

        .gpu-name {
            font-size: var(--font-size-lg, 17px);
            font-weight: 600;
            color: var(--text-primary, #000);
        }

        .gpu-index {
            font-size: var(--font-size-xs, 11px);
            color: var(--text-tertiary, #999);
            font-family: var(--font-mono, monospace);
            padding: 2px 8px;
            background: var(--bg-tertiary, #f5f5f5);
            border-radius: var(--radius-full, 9999px);
        }
    `;

    render() {
        // Use power limit fallback of 300 when nvidia-smi reports 0 (i.e.
        // [N/A]). The dashboard still needs a meaningful max so the bar
        // isn't permanently pinned.
        const powerMax = (this.powerLimitW > 0) ? this.powerLimitW : 300;
        const memMax = (this.memoryTotalMib > 0) ? this.memoryTotalMib : 24576;

        const m = this.metrics || {};

        return html`
            <div class="card-header">
                <span class="gpu-name">${this.gpuName}</span>
                <span class="gpu-index">GPU ${this.gpuIndex}</span>
            </div>

            <gpu-gauge
                metric="temperature"
                label="Temperature"
                .value=${m.temperature ?? 0}
                max="100"
                unit="°C">
            </gpu-gauge>

            <gpu-gauge
                metric="utilization"
                label="GPU Utilization"
                .value=${m.utilization ?? 0}
                max="100"
                unit="%">
            </gpu-gauge>

            <gpu-gauge
                metric="memory"
                label="Memory"
                .value=${m.memory ?? 0}
                .max=${memMax}
                unit="MiB">
            </gpu-gauge>

            <gpu-gauge
                metric="power"
                label="Power"
                .value=${m.power ?? 0}
                .max=${powerMax}
                unit="W">
            </gpu-gauge>
        `;
    }
}

customElements.define('gpu-card', GpuCard);
