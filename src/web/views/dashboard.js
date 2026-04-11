/*
 * views/dashboard.js — Dashboard view.
 *
 * Phase 4. Three sections:
 *   1. Multi-GPU picker (only visible when gpus.length > 1)
 *   2. Grid of <gpu-card> elements, one per GPU (or just one in
 *      single-GPU mode)
 *   3. Historical chart for the currently-selected GPU + time range
 *
 * Polls /api/metrics/current every 4 seconds and rebuilds the gauges,
 * and polls /api/metrics/history on time-range change (not on a
 * fixed cadence). Chart.js 4 is used for the chart — loaded from the
 * CDN just like Lit.
 *
 * View lifecycle: mount() runs on entry, sets up the DOM + polling
 * interval; unmount() tears down the interval and destroys the
 * Chart.js instance so the view can be re-entered cleanly.
 */

import * as api from '../api.js';
import '../components/gauge.js';
import '../components/gpu-card.js';

const TIME_RANGES = [
    { id: '15m', label: '15m' },
    { id: '30m', label: '30m' },
    { id: '1h',  label: '1 h' },
    { id: '6h',  label: '6 h' },
    { id: '12h', label: '12 h' },
    { id: '24h', label: '24 h' },
    { id: '3d',  label: '3 d' },
];

let pollInterval = null;
let chartInstance = null;
let themeChangeHandler = null;
let state = {
    gpus: [],
    selectedGpuIndex: 0,
    currentMetrics: [],
    timeRange: '1h',
};

/* ─── DOM builders ─────────────────────────────────────────────────────── */

function buildHeader() {
    const header = document.createElement('header');

    const title = document.createElement('h1');
    title.textContent = 'Dashboard';

    const subtitle = document.createElement('div');
    subtitle.className = 'subtitle';
    subtitle.textContent = 'Real-time GPU telemetry';

    header.append(title, subtitle);
    return header;
}

function buildGpuTabs() {
    if (state.gpus.length <= 1) return null;

    const wrapper = document.createElement('section');
    const label = document.createElement('div');
    label.style.marginBottom = '12px';
    label.style.color = 'var(--text-tertiary)';
    label.style.fontSize = 'var(--font-size-sm)';
    label.textContent = `${state.gpus.length} GPUs attached`;

    const tabs = document.createElement('div');
    tabs.className = 'tabs';
    tabs.setAttribute('role', 'tablist');

    state.gpus.forEach((gpu) => {
        const btn = document.createElement('button');
        btn.textContent = `GPU ${gpu.index}`;
        btn.setAttribute('data-gpu-index', String(gpu.index));
        btn.setAttribute('role', 'tab');
        if (gpu.index === state.selectedGpuIndex) {
            btn.setAttribute('aria-current', 'true');
        }
        btn.addEventListener('click', () => {
            state.selectedGpuIndex = gpu.index;
            // Refresh the active tab highlight
            tabs.querySelectorAll('button').forEach(b => {
                if (b.getAttribute('data-gpu-index') === String(gpu.index)) {
                    b.setAttribute('aria-current', 'true');
                } else {
                    b.removeAttribute('aria-current');
                }
            });
            // Reload the history chart for the newly-selected GPU
            refreshHistory();
        });
        tabs.append(btn);
    });

    wrapper.append(label, tabs);
    return wrapper;
}

function buildGpuCards(container) {
    const grid = document.createElement('div');
    grid.className = 'card-grid wide';
    grid.id = 'gpu-cards-grid';

    state.gpus.forEach((gpu) => {
        const metrics = state.currentMetrics.find(m => m.gpu_index === gpu.index) || {};
        const card = document.createElement('gpu-card');
        card.setAttribute('gpu-index', String(gpu.index));
        card.setAttribute('gpu-name', gpu.name || 'GPU');
        card.setAttribute('memory-total-mib', String(gpu.memory_total_mib || 24576));
        card.setAttribute('power-limit-w', String(gpu.power_limit_w || 0));
        card.metrics = {
            temperature: metrics.temperature ?? 0,
            utilization: metrics.utilization ?? 0,
            memory: metrics.memory ?? 0,
            power: metrics.power ?? 0,
        };
        grid.append(card);
    });

    container.append(grid);
}

function buildTimeRangePicker() {
    const picker = document.createElement('div');
    picker.className = 'time-range';

    TIME_RANGES.forEach((range) => {
        const btn = document.createElement('button');
        btn.textContent = range.label;
        btn.setAttribute('data-range', range.id);
        if (range.id === state.timeRange) {
            btn.setAttribute('aria-current', 'true');
        }
        btn.addEventListener('click', () => {
            state.timeRange = range.id;
            picker.querySelectorAll('button').forEach(b => {
                if (b.getAttribute('data-range') === range.id) {
                    b.setAttribute('aria-current', 'true');
                } else {
                    b.removeAttribute('aria-current');
                }
            });
            refreshHistory();
        });
        picker.append(btn);
    });

    return picker;
}

function buildChartCard() {
    const card = document.createElement('section');
    card.className = 'card';
    card.id = 'history-card';

    const header = document.createElement('header');
    const h3 = document.createElement('h3');
    h3.textContent = 'Historical trend';
    const subtitle = document.createElement('div');
    subtitle.className = 'subtitle';
    subtitle.textContent = 'Selected GPU over the chosen time range';

    const headerLeft = document.createElement('div');
    headerLeft.append(h3, subtitle);

    header.append(headerLeft, buildTimeRangePicker());
    card.append(header);

    const container = document.createElement('div');
    container.className = 'chart-container';
    const canvas = document.createElement('canvas');
    canvas.id = 'history-chart';
    container.append(canvas);
    card.append(container);

    return card;
}

/* ─── Data refresh ─────────────────────────────────────────────────────── */

async function refreshCurrent() {
    state.currentMetrics = await api.getCurrentMetrics();

    // Update each <gpu-card> in place — Lit re-renders on property change.
    const grid = document.getElementById('gpu-cards-grid');
    if (!grid) return;

    state.gpus.forEach((gpu) => {
        const card = grid.querySelector(`gpu-card[gpu-index="${gpu.index}"]`);
        if (!card) return;
        const metrics = state.currentMetrics.find(m => m.gpu_index === gpu.index) || {};
        card.metrics = {
            temperature: metrics.temperature ?? 0,
            utilization: metrics.utilization ?? 0,
            memory: metrics.memory ?? 0,
            power: metrics.power ?? 0,
        };
    });
}

// Read the current theme's chart-relevant CSS custom properties into a plain
// JS object. Chart.js cannot dereference `var(--…)` values itself, so we
// snapshot them once per call and spread into the options tree below.
function readThemeColors() {
    const cs = getComputedStyle(document.documentElement);
    const get = (name) => cs.getPropertyValue(name).trim();
    return {
        textPrimary:   get('--text-primary'),
        textSecondary: get('--text-secondary'),
        textTertiary:  get('--text-tertiary'),
        bgSecondary:   get('--bg-secondary'),
        borderRegular: get('--border-regular'),
        borderSubtle:  get('--border-subtle'),
    };
}

// Build a fresh Chart.js options object coloured with the current theme.
// Pure function: no side effects, no DOM mutation, no Chart.js calls.
function buildChartOptions() {
    const c = readThemeColors();
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: {
                position: 'top',
                labels: {
                    color: c.textSecondary,
                    font: { size: 12 },
                },
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
                ticks: {
                    maxTicksLimit: 8,
                    autoSkip: true,
                    color: c.textTertiary,
                },
                grid: { color: c.borderSubtle },
            },
            y: {
                beginAtZero: true,
                ticks: { color: c.textTertiary },
                grid: { color: c.borderSubtle },
            },
        },
    };
}

// Re-skin the existing chart instance when the theme flips. No data refetch,
// no network/DB hit — just walk the options tree with fresh CSS-var values
// and let Chart.js re-render with update('none') so datasets don't animate.
// This is the cheap alternative to calling refreshHistory() on themechange.
function applyChartThemeOptions() {
    if (!chartInstance) return;
    chartInstance.options = buildChartOptions();
    chartInstance.update('none');
}

async function refreshHistory() {
    const history = await api.getHistory(state.timeRange, state.selectedGpuIndex);

    const canvas = document.getElementById('history-chart');
    if (!canvas) return;

    const ds = (label, data, color, hidden = false) => ({
        label,
        data,
        borderColor: color,
        backgroundColor: color + '22',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.3,
        hidden,
    });

    const datasets = [
        ds('Temperature (°C)', history.temperatures, '#ff3b30'),
        ds('Utilization (%)',  history.utilizations, '#34c759'),
        ds('Memory (MiB)',     history.memory,       '#007aff', true),
        ds('Power (W)',        history.power,        '#af52de', true),
    ];

    const data = {
        labels: history.timestamps,
        datasets,
    };

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
        console.warn('dashboard: Chart.js not available');
    }
}

/* ─── View lifecycle ───────────────────────────────────────────────────── */

export const dashboardView = {
    name: 'dashboard',

    async mount(container) {
        // Fetch GPU inventory once — the inventory is stable per session
        // (hot-add/remove requires a container restart per Phase 2 scope).
        // Sort by index so "the lowest-index GPU" comment in the default
        // selection below is structurally guaranteed, not just
        // accidentally true because nvidia-smi happens to return rows in
        // index order.
        const fetched = await api.getGpus();
        state.gpus = [...fetched].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
        if (state.gpus.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'empty-state';
            const icon = document.createElement('div');
            icon.className = 'icon';
            icon.textContent = '🖥️';
            const title = document.createElement('div');
            title.className = 'title';
            title.textContent = 'No GPUs detected';
            const desc = document.createElement('div');
            desc.className = 'description';
            desc.textContent = 'The collector reported no attached NVIDIA GPUs. Check that nvidia-smi is available inside the container and that --gpus all was passed on docker run.';
            empty.append(icon, title, desc);
            container.append(empty);
            return;
        }

        // Default-select the first (lowest-index) GPU
        state.selectedGpuIndex = state.gpus[0].index;

        // Build the static structure
        container.append(buildHeader());

        const tabs = buildGpuTabs();
        if (tabs) container.append(tabs);

        buildGpuCards(container);
        container.append(buildChartCard());

        // Initial data fill
        await refreshCurrent();
        await refreshHistory();

        // Poll current metrics every 4 seconds — matches the default
        // collector interval. A more ambitious Phase 5+ implementation
        // could switch this to SSE for push updates; 4s polling is
        // perfectly fine for a homelab dashboard.
        pollInterval = setInterval(() => {
            refreshCurrent().catch(err => console.warn('dashboard poll failed:', err));
        }, 4000);

        // Re-skin the chart when the theme flips so the legend / tooltip /
        // tick colors follow the new palette. Calls applyChartThemeOptions()
        // which rebuilds only the options tree from current CSS custom
        // properties — no /api/metrics/history refetch, no dataset reset,
        // no visible flicker. Without this, toggling the sidebar theme
        // button would leave the chart stuck in the prior theme's colors
        // until the user changed the time range or GPU tab.
        themeChangeHandler = () => {
            try {
                applyChartThemeOptions();
            } catch (err) {
                console.warn('dashboard: theme-change re-skin failed:', err);
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
