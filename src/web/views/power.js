/*
 * views/power.js — Power usage view.
 *
 * Phase 5. Per-GPU power draw, integrated energy, and electricity cost.
 *
 * Structure:
 *   1. GPU picker (tabs) — only rendered when gpus.length > 1
 *   2. Window picker — 1h / 24h / 7d / 30d buttons
 *   3. KPI row — three tiles: energy (Wh/kWh), peak power, cost
 *   4. Power timeseries chart for the selected GPU + window
 *
 * Data sources:
 *   - /api/gpus                                            — inventory for the picker
 *   - /api/stats/power?range=<window>&gpu=<index>          — the SUM query
 *   - /api/metrics/history?range=<window>&gpu=<index>      — the chart series
 *
 * The cost calculation is `(energy_wh / 1000) * rate` where `rate` is an
 * electricity tariff per kWh. Phase 5 stubs this at 0 with an info-tip
 * explaining that the rate is configured in Settings — Phase 6 will add
 * /api/settings and wire the real value in. The tile is still rendered
 * (showing "$0.00") so users can see where the cost will appear without
 * a placeholder wall.
 *
 * Chart.js instance is stored on a module-local variable, not shared
 * with the Dashboard view, so each view tears down its own chart on
 * unmount without stepping on the other.
 */

import * as api from '../api.js';
import '../components/info-tip.js';
import { attachTablistKeyboard, markTabSelected } from '../widgets/tablist.js';

const WINDOWS = [
    { id: '1h',  label: '1 h' },
    { id: '24h', label: '24 h' },
    { id: '7d',  label: '7 d'  },
    { id: '30d', label: '30 d' },
];

// The Power view polls less often than the Dashboard — energy is a
// SUM over minutes-to-days of samples, so hitting the API every 4 s
// wastes cycles re-computing a number that barely changes. 30 s
// strikes a balance between "see the number updating" and "don't
// hammer the DB".
const POLL_MS = 30_000;

let chartInstance = null;
let pollInterval = null;
let themeChangeHandler = null;
let state = {
    gpus: [],
    selectedGpuIndex: 0,
    window: '24h',
    // Electricity rate placeholder. Phase 6 reads this from
    // /api/settings.power.rate_per_kwh; Phase 5 hardcodes 0 so the
    // cost tile renders "$0.00" as a shape placeholder rather than
    // being hidden entirely.
    electricityRate: 0,
    currency: '$',
};

/* ─── Formatting helpers ────────────────────────────────────────────────── */

function formatEnergy(wh) {
    // Sub-kWh: show as Wh with 0 decimals; kWh: show with 2 decimals.
    // Matches how home electricity meters display consumption.
    if (!Number.isFinite(wh) || wh < 0) return { value: '—', unit: 'Wh' };
    if (wh < 1000) return { value: wh.toFixed(0), unit: 'Wh' };
    return { value: (wh / 1000).toFixed(2), unit: 'kWh' };
}

function formatWatts(w) {
    if (!Number.isFinite(w) || w < 0) return { value: '—', unit: 'W' };
    return { value: w.toFixed(1), unit: 'W' };
}

function formatCost(wh, ratePerKwh, currency) {
    if (!Number.isFinite(wh) || wh < 0) return `${currency}—`;
    const cost = (wh / 1000) * ratePerKwh;
    return `${currency}${cost.toFixed(2)}`;
}

/* ─── Chart theming (shared pattern with dashboard.js) ──────────────────── */

function readThemeColors() {
    const cs = getComputedStyle(document.documentElement);
    const get = (name) => cs.getPropertyValue(name).trim();
    return {
        textSecondary: get('--text-secondary'),
        textTertiary:  get('--text-tertiary'),
        textPrimary:   get('--text-primary'),
        bgSecondary:   get('--bg-secondary'),
        borderRegular: get('--border-regular'),
        borderSubtle:  get('--border-subtle'),
    };
}

function buildChartOptions() {
    const c = readThemeColors();
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: {
                position: 'top',
                labels: { color: c.textSecondary, font: { size: 12 } },
            },
            tooltip: {
                backgroundColor: c.bgSecondary,
                titleColor:      c.textPrimary,
                bodyColor:       c.textSecondary,
                borderColor:     c.borderRegular,
                borderWidth: 1,
            },
        },
        scales: {
            x: {
                ticks: { maxTicksLimit: 8, autoSkip: true, color: c.textTertiary },
                grid: { color: c.borderSubtle },
            },
            y: {
                beginAtZero: true,
                ticks: {
                    color: c.textTertiary,
                    callback: (v) => `${v} W`,
                },
                grid: { color: c.borderSubtle },
            },
        },
    };
}

function applyChartThemeOptions() {
    if (!chartInstance) return;
    chartInstance.options = buildChartOptions();
    chartInstance.update('none');
}

/* ─── DOM builders ──────────────────────────────────────────────────────── */

function buildHeader() {
    const header = document.createElement('header');
    const h1 = document.createElement('h1');
    h1.textContent = 'Power usage';
    const subtitle = document.createElement('div');
    subtitle.className = 'subtitle';
    // Fold the GPU count into the page subtitle, matching the Dashboard
    // pattern — avoids a standalone "N GPUs attached" banner and the
    // vertical clutter that came with it.
    const count = state.gpus.length;
    const attachedSuffix = count > 1 ? ` · ${count} GPUs attached` : '';
    subtitle.textContent =
        'Per-GPU power draw, integrated energy, and electricity cost'
        + attachedSuffix;
    header.append(h1, subtitle);
    return header;
}

// Single consolidated control row: GPU picker on the left (multi-GPU
// only), window picker on the right. Matches the Dashboard's
// "subject-left, modifier-right" LTR pattern and collapses two
// previously-separate labelled sections into one visual cluster,
// eliminating ~80px of non-uniform vertical spacing that the old
// two-section layout introduced.
//
// Returns the wrapper element (a <section>) so it still participates
// in the `main.content section { margin-bottom: var(--space-6); }`
// spacing rule.
function buildControls(onGpuSelect, onWindowChange) {
    const wrapper = document.createElement('section');
    wrapper.style.display = 'flex';
    wrapper.style.flexWrap = 'wrap';
    wrapper.style.justifyContent = 'space-between';
    wrapper.style.alignItems = 'center';
    wrapper.style.gap = 'var(--space-3)';

    // Left cluster — GPU tabs (or an empty div when single-GPU so the
    // window picker stays right-aligned even without a GPU selector).
    const left = document.createElement('div');
    if (state.gpus.length > 1) {
        const tabs = document.createElement('div');
        tabs.className = 'tabs';
        tabs.setAttribute('role', 'tablist');
        tabs.setAttribute('aria-label', 'Select GPU for power integration');

        let initialActive = null;
        state.gpus.forEach((gpu) => {
            const btn = document.createElement('button');
            btn.textContent = `GPU ${gpu.index}`;
            btn.setAttribute('data-gpu-index', String(gpu.index));
            btn.setAttribute('role', 'tab');
            btn.addEventListener('click', () => {
                state.selectedGpuIndex = gpu.index;
                markTabSelected(tabs, btn);
                onGpuSelect();
            });
            tabs.append(btn);
            if (gpu.index === state.selectedGpuIndex) initialActive = btn;
        });

        if (initialActive) markTabSelected(tabs, initialActive);
        attachTablistKeyboard(tabs, {
            onSelect: (targetTab) => {
                const idx = Number(targetTab.getAttribute('data-gpu-index'));
                if (Number.isFinite(idx)) {
                    state.selectedGpuIndex = idx;
                    onGpuSelect();
                }
            },
        });
        left.append(tabs);
    }

    // Right cluster — integration window picker.
    const picker = document.createElement('div');
    picker.className = 'time-range';
    picker.setAttribute('role', 'tablist');
    picker.setAttribute('aria-label', 'Integration window');

    let initialActiveWin = null;
    WINDOWS.forEach((w) => {
        const btn = document.createElement('button');
        btn.textContent = w.label;
        btn.setAttribute('data-window', w.id);
        btn.setAttribute('role', 'tab');
        btn.addEventListener('click', () => {
            state.window = w.id;
            markTabSelected(picker, btn);
            onWindowChange();
        });
        picker.append(btn);
        if (w.id === state.window) initialActiveWin = btn;
    });

    if (initialActiveWin) markTabSelected(picker, initialActiveWin);
    attachTablistKeyboard(picker, {
        onSelect: (targetTab) => {
            const id = targetTab.getAttribute('data-window');
            if (id) {
                state.window = id;
                onWindowChange();
            }
        },
    });

    wrapper.append(left, picker);
    return wrapper;
}

/* ─── KPI tile (reusable) ───────────────────────────────────────────────── */

function buildKpiCard(id, title, infoText) {
    const card = document.createElement('section');
    // `compact` drops internal padding from 32px → 16px so the tiles
    // don't feel puffy around their short single-number content.
    card.className = 'card compact';
    card.id = id;

    const header = document.createElement('header');
    const h3 = document.createElement('h3');
    h3.textContent = title;
    header.append(h3);

    if (infoText) {
        const tip = document.createElement('info-tip');
        tip.setAttribute('text', infoText);
        header.append(tip);
    }
    card.append(header);

    // Big value + small unit. Inline styles keep the tile self-contained
    // — no new CSS class needed in components.css for three tiles used
    // in exactly one view. Phase 7 can promote this to a shared .kpi
    // class if more views start using it.
    const value = document.createElement('div');
    value.className = 'value';
    value.style.fontSize = 'var(--font-size-3xl)';
    value.style.fontWeight = 'var(--font-weight-bold)';
    value.style.fontVariantNumeric = 'tabular-nums';
    value.style.color = 'var(--text-primary)';
    value.style.lineHeight = '1.1';
    value.textContent = '—';

    const unit = document.createElement('span');
    unit.className = 'unit';
    unit.style.fontSize = 'var(--font-size-md)';
    unit.style.fontWeight = 'var(--font-weight-normal)';
    unit.style.color = 'var(--text-tertiary)';
    unit.style.marginLeft = 'var(--space-2)';
    value.append(unit);

    const sub = document.createElement('div');
    sub.className = 'subtitle';
    sub.style.marginTop = 'var(--space-2)';
    sub.style.color = 'var(--text-tertiary)';
    sub.style.fontSize = 'var(--font-size-sm)';
    sub.textContent = '';

    card.append(value, sub);
    return card;
}

function updateKpiCard(cardEl, displayValue, displayUnit, subtitle) {
    const valueEl = cardEl.querySelector('.value');
    if (!valueEl) return;
    // Clear the text node before the <span>, preserving the unit span.
    const unitEl = valueEl.querySelector('.unit');
    valueEl.textContent = displayValue;
    if (unitEl) {
        unitEl.textContent = displayUnit || '';
        valueEl.append(unitEl);
    }
    const subEl = cardEl.querySelector('.subtitle');
    if (subEl) subEl.textContent = subtitle || '';
}

function buildChartCard() {
    const card = document.createElement('section');
    // `compact` drops the card's internal padding so the chart's
    // built-in 320px height + the card's previous 64px vertical
    // padding don't stack into a 400px block that feels oversized
    // relative to the three short KPI tiles above.
    card.className = 'card compact';
    card.id = 'power-chart-card';

    const header = document.createElement('header');
    const h3 = document.createElement('h3');
    h3.textContent = 'Power draw';
    const subtitle = document.createElement('div');
    subtitle.className = 'subtitle';
    subtitle.id = 'power-chart-subtitle';
    subtitle.textContent = 'Instantaneous power over the selected window';

    const left = document.createElement('div');
    left.append(h3, subtitle);
    header.append(left);
    card.append(header);

    const wrap = document.createElement('div');
    wrap.className = 'chart-container';
    // Power view uses a shorter chart than the default 320px — the
    // KPI tiles above are short, so a tall chart feels disconnected.
    // 240px keeps enough vertical resolution for the power curve
    // while preserving the "single integrated panel" feel.
    wrap.style.height = '240px';
    const canvas = document.createElement('canvas');
    canvas.id = 'power-history-chart';
    wrap.append(canvas);
    card.append(wrap);

    return card;
}

/* ─── Data refresh ──────────────────────────────────────────────────────── */

// The chart shows raw per-sample power, so its window must stay small
// enough that the JSON payload and Chart.js point array don't crush the
// browser. 30 days × 4s sampling × per-GPU = ~648k points — unusably
// large. The integrated SUM tile (which only returns eight floats
// regardless of window) is the one that actually supports 30d. When the
// user picks 30d, cap the chart fetch at 7d and annotate the chart
// subtitle so they know the chart and the KPI window have diverged.
const CHART_MAX_WINDOW = '7d';

function chartWindow() {
    return state.window === '30d' ? CHART_MAX_WINDOW : state.window;
}

async function refresh() {
    const fetchChartWindow = chartWindow();
    const [stats, history] = await Promise.all([
        api.getPowerStats(state.window, state.selectedGpuIndex),
        api.getHistory(fetchChartWindow, state.selectedGpuIndex),
    ]);

    // Update KPI tiles
    const energyCard = document.getElementById('kpi-energy');
    const peakCard   = document.getElementById('kpi-peak');
    const costCard   = document.getElementById('kpi-cost');

    if (energyCard) {
        const e = formatEnergy(stats.energy_wh);
        const sub = stats.insufficient_telemetry
            ? `${stats.samples_invalid} of ${stats.samples_total} samples missing power — lower bound`
            : `Integrated from ${stats.samples_total} samples`;
        updateKpiCard(energyCard, e.value, e.unit, sub);
    }

    if (peakCard) {
        const p = formatWatts(stats.peak_power_w);
        const avg = formatWatts(stats.avg_power_w);
        updateKpiCard(peakCard, p.value, p.unit, `Average ${avg.value} ${avg.unit}`);
    }

    if (costCard) {
        const cost = formatCost(stats.energy_wh, state.electricityRate, state.currency);
        const sub = state.electricityRate > 0
            ? `At ${state.currency}${state.electricityRate.toFixed(4)}/kWh`
            : 'Set your electricity rate in Settings';
        updateKpiCard(costCard, cost, '', sub);
    }

    // Update chart subtitle when the chart window diverges from the KPI
    // window (i.e. the user picked 30d but we capped the chart at 7d).
    const chartSub = document.getElementById('power-chart-subtitle');
    if (chartSub) {
        chartSub.textContent = (state.window === '30d')
            ? `Instantaneous power — last ${CHART_MAX_WINDOW} (KPI tiles use the full 30 d)`
            : 'Instantaneous power over the selected window';
    }

    // Update chart
    const canvas = document.getElementById('power-history-chart');
    if (!canvas) return;

    const dataset = {
        label: `GPU ${state.selectedGpuIndex} power (W)`,
        data: history.power,
        borderColor: '#af52de',
        backgroundColor: '#af52de33',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.3,
        fill: true,
    };

    const data = { labels: history.timestamps, datasets: [dataset] };
    const options = buildChartOptions();

    if (chartInstance) {
        chartInstance.data = data;
        chartInstance.options = options;
        chartInstance.update('none');
    } else if (window.Chart) {
        chartInstance = new window.Chart(canvas.getContext('2d'), {
            type: 'line',
            data,
            options,
        });
    } else {
        console.warn('power: Chart.js not available');
    }
}

/* ─── View lifecycle ────────────────────────────────────────────────────── */

export const powerView = {
    name: 'power',

    async mount(container) {
        // Fetch the inventory once; it's stable per container session.
        const fetched = await api.getGpus();
        state.gpus = [...fetched].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
        if (state.gpus.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            const icon = document.createElement('div');
            icon.className = 'icon';
            icon.textContent = '⚡';
            const title = document.createElement('div');
            title.className = 'title';
            title.textContent = 'No GPUs detected';
            const desc = document.createElement('div');
            desc.className = 'description';
            desc.textContent = 'The collector reported no attached NVIDIA GPUs. Power statistics require a working nvidia-smi inside the container.';
            empty.append(icon, title, desc);
            container.append(empty);
            return;
        }

        state.selectedGpuIndex = state.gpus[0].index;

        // Build the static structure. Header folds the GPU count into
        // the subtitle; controls row merges what used to be two
        // labelled sections into a single flex row with GPU tabs left
        // and window picker right.
        //
        // Vertical gaps between the controls → KPI row → chart card
        // are tightened from the global 32px (--space-6) default
        // down to 16px (--space-4), matching the horizontal gap
        // between the three KPI tiles. This makes the Power view
        // feel like a single integrated panel rather than four
        // loosely-stacked sections — a Power-specific override
        // because the KPI tiles are shorter than the Dashboard's
        // gauge cards, so the global gap was disproportionate to
        // their height.
        container.append(buildHeader());

        const controls = buildControls(() => refresh(), () => refresh());
        controls.style.marginBottom = 'var(--space-4)';
        container.append(controls);

        // KPI row
        const kpiGrid = document.createElement('div');
        kpiGrid.className = 'card-grid';
        kpiGrid.style.marginBottom = 'var(--space-4)';
        kpiGrid.append(
            buildKpiCard(
                'kpi-energy',
                'Energy',
                'Integrated power over the selected window. Each sample contributes power × interval_s, summed and divided by 3600. Missing-telemetry samples are excluded, so this is a lower bound when some readings were unavailable.',
            ),
            buildKpiCard(
                'kpi-peak',
                'Peak power',
                'Highest single sample in the window, with the mean shown below. Zero-power or missing samples are excluded from both.',
            ),
            buildKpiCard(
                'kpi-cost',
                'Estimated cost',
                'Energy × electricity rate per kWh. Set your local rate in Settings → Power. Until you do, this shows zero as a placeholder.',
            ),
        );
        container.append(kpiGrid);

        container.append(buildChartCard());

        // Initial data fill
        await refresh();

        // Poll every POLL_MS. Longer than the dashboard because a SUM
        // over an entire time window doesn't meaningfully change every
        // 4 seconds — and the chart only refreshes when the window or
        // GPU changes, which already call refresh() explicitly.
        pollInterval = setInterval(() => {
            refresh().catch(err => console.warn('power poll failed:', err));
        }, POLL_MS);

        // Re-skin the chart on theme flip. Cheap — no re-fetch.
        themeChangeHandler = () => {
            try {
                applyChartThemeOptions();
            } catch (err) {
                console.warn('power: theme-change re-skin failed:', err);
            }
        };
        window.addEventListener('themechange', themeChangeHandler);
    },

    unmount() {
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
        if (chartInstance) {
            chartInstance.destroy();
            chartInstance = null;
        }
        if (themeChangeHandler) {
            window.removeEventListener('themechange', themeChangeHandler);
            themeChangeHandler = null;
        }
    },
};
