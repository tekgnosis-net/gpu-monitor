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
import * as alerts from '../alerts.js';
import '../components/gauge.js';
import '../components/gpu-card.js';
import { attachTablistKeyboard, markTabSelected } from '../widgets/tablist.js';

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
let visibilityHandler = null;
let state = {
    gpus: [],
    selectedGpuIndex: 0,
    currentMetrics: [],
    timeRange: '1h',
    consecutiveFailures: 0,
};

/* ─── DOM builders ─────────────────────────────────────────────────────── */

function buildHeader() {
    const header = document.createElement('header');

    const title = document.createElement('h1');
    title.textContent = 'Dashboard';

    const subtitle = document.createElement('div');
    subtitle.className = 'subtitle';
    // Fold the per-GPU count into the page subtitle so there's no
    // standalone "N GPUs attached" banner floating above the cards.
    // The cards themselves are already labelled "GPU 0 / GPU 1 /…"
    // in their own headers (via gpu-card.js), which is the canonical
    // "which card is which" anchor.
    const count = state.gpus.length;
    subtitle.textContent = count > 1
        ? `Real-time GPU telemetry · ${count} GPUs attached`
        : 'Real-time GPU telemetry';

    header.append(title, subtitle);
    return header;
}

// Build a GPU-selection tablist for the chart card header. Returns null
// in single-GPU mode — the single-card case has nothing to select
// between, so the chart unambiguously tracks GPU 0 and the tablist is
// hidden entirely. In multi-GPU mode this lives INSIDE the chart
// card header next to the time-range picker, so the spatial relationship
// "these controls govern this chart" is obvious without a text label.
function buildChartGpuTabs() {
    if (state.gpus.length <= 1) return null;

    const tabs = document.createElement('div');
    tabs.className = 'tabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Select GPU for chart');

    let initialActive = null;
    state.gpus.forEach((gpu) => {
        const btn = document.createElement('button');
        btn.textContent = `GPU ${gpu.index}`;
        btn.setAttribute('data-gpu-index', String(gpu.index));
        btn.setAttribute('role', 'tab');
        btn.addEventListener('click', () => {
            state.selectedGpuIndex = gpu.index;
            markTabSelected(tabs, btn);
            refreshHistory();
        });
        tabs.append(btn);
        if (gpu.index === state.selectedGpuIndex) {
            initialActive = btn;
        }
    });

    // Phase 7 / task #28: full WAI-ARIA tab pattern via
    // widgets/tablist.js — aria-selected, roving tabindex,
    // arrow-key navigation.
    if (initialActive) markTabSelected(tabs, initialActive);
    attachTablistKeyboard(tabs, {
        onSelect: (targetTab) => {
            const idx = Number(targetTab.getAttribute('data-gpu-index'));
            if (Number.isFinite(idx)) {
                state.selectedGpuIndex = idx;
                refreshHistory();
            }
        },
    });

    return tabs;
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
    picker.setAttribute('role', 'tablist');
    picker.setAttribute('aria-label', 'Chart time range');

    let initialActive = null;
    TIME_RANGES.forEach((range) => {
        const btn = document.createElement('button');
        btn.textContent = range.label;
        btn.setAttribute('data-range', range.id);
        btn.setAttribute('role', 'tab');
        btn.addEventListener('click', () => {
            state.timeRange = range.id;
            markTabSelected(picker, btn);
            refreshHistory();
        });
        picker.append(btn);
        if (range.id === state.timeRange) initialActive = btn;
    });

    if (initialActive) markTabSelected(picker, initialActive);
    attachTablistKeyboard(picker, {
        onSelect: (targetTab) => {
            const id = targetTab.getAttribute('data-range');
            if (id) {
                state.timeRange = id;
                refreshHistory();
            }
        },
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
    subtitle.textContent = state.gpus.length > 1
        ? 'Selected GPU over the chosen time range'
        : 'Over the chosen time range';

    // Left side of the header holds the heading + subtitle and, in
    // multi-GPU mode, the GPU selector directly below — creating a
    // vertical "what am I looking at" reading order:
    //    Historical trend   (heading: what is this card)
    //    Selected GPU ...   (subtitle: how to interpret)
    //    [GPU 0] [GPU 1]    (subject: which one is currently shown)
    // This matches LTR reading conventions where the subject of a
    // control group lives on the left and the modifier (time range)
    // lives on the right, like a music player's track label vs
    // transport controls.
    const headerLeft = document.createElement('div');
    headerLeft.style.display = 'flex';
    headerLeft.style.flexDirection = 'column';
    headerLeft.style.gap = 'var(--space-2)';
    headerLeft.append(h3, subtitle);

    const gpuTabs = buildChartGpuTabs();
    if (gpuTabs) {
        // Align the tab strip flush with the heading's left edge so
        // it reads as "owned by" the heading above it rather than as
        // a floating control.
        gpuTabs.style.alignSelf = 'flex-start';
        headerLeft.append(gpuTabs);
    }

    // Right side keeps only the time-range picker — the consistent
    // "operation" slot that Power, Settings, etc. also right-align.
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
    const fetched = await api.getCurrentMetrics();

    // Track connection health: an empty array from getCurrentMetrics
    // means the fetch failed or the DB returned no rows. After 3
    // consecutive empty responses, show a staleness indicator in the
    // header subtitle so the user knows the data may be stale. A
    // single successful response resets the counter immediately.
    if (!fetched || fetched.length === 0) {
        state.consecutiveFailures++;
        updateConnectionStatus();
        return;  // keep the last-known metrics on screen
    }
    state.consecutiveFailures = 0;
    state.currentMetrics = fetched;
    updateConnectionStatus();

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

    // Phase 7: hand the fresh metrics to the alert state machine.
    // It compares each per-GPU value against the thresholds loaded
    // at mount from /api/settings.alerts and fires a toast/sound/
    // notification on breach. Cooldown logic is inside alerts.js —
    // dashboard.js just needs to call it on every poll.
    try {
        alerts.checkMetrics(state.currentMetrics, state.gpus);
    } catch (err) {
        console.warn('dashboard: alert check failed:', err);
    }
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

/* ─── Connection health ────────────────────────────────────────────────── */

// Show/hide a staleness warning in the Dashboard subtitle when
// multiple consecutive poll failures occur. This tells the user
// "the data you see may be stale" rather than silently displaying
// old numbers. The warning clears immediately on the first
// successful response.
function updateConnectionStatus() {
    const subtitle = document.querySelector('main.content header .subtitle');
    if (!subtitle) return;

    const count = state.gpus.length;
    const base = count > 1
        ? `Real-time GPU telemetry · ${count} GPUs attached`
        : 'Real-time GPU telemetry';

    if (state.consecutiveFailures >= 3) {
        subtitle.textContent = base + ' · ⚠ Connection lost — retrying…';
        subtitle.style.color = 'var(--danger, #ff3b30)';
    } else {
        subtitle.textContent = base;
        subtitle.style.color = '';  // reset to default CSS
    }
}

// Pause/resume polling based on tab visibility. When the tab is
// hidden (user switched to another tab or minimized), stop polling
// to save bandwidth and server load. When the tab becomes visible
// again, fire an immediate refresh to recover from any staleness,
// then restart the interval.
function startPolling() {
    if (pollInterval) return;  // already running
    pollInterval = setInterval(() => {
        refreshCurrent().catch(err => console.warn('dashboard poll failed:', err));
    }, 4000);
}

function stopPolling() {
    if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
    }
}

function handleVisibilityChange() {
    if (document.hidden) {
        stopPolling();
    } else {
        // Tab became visible — immediate refresh to recover from
        // any staleness accumulated while hidden, then restart the
        // regular 4s cadence.
        refreshCurrent().catch(err => console.warn('dashboard: visibility refresh failed:', err));
        startPolling();
    }
}

/* ─── View lifecycle ───────────────────────────────────────────────────── */

export const dashboardView = {
    name: 'dashboard',

    async mount(container) {
        // Phase 7: load alert thresholds from /api/settings.alerts
        // before the first poll arrives. The alerts module falls
        // back to pre-Phase-4 hardcoded defaults (80/100/300) on
        // any fetch failure, so this call is "best effort" and
        // never blocks mount on the alerts path.
        alerts.loadThresholdsFromServer().catch(() => { /* fallback ok */ });

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

        // Build the static structure. Note the GPU selector no longer
        // lives at the top of the page — it's collocated with the chart
        // it actually controls, inside buildChartCard()'s header.
        container.append(buildHeader());
        buildGpuCards(container);
        container.append(buildChartCard());

        // Initial data fill
        await refreshCurrent();
        await refreshHistory();

        // Poll current metrics every 4 seconds — matches the default
        // collector interval. Visibility-aware: pauses when the tab is
        // hidden (saves bandwidth), resumes with an immediate refresh
        // when the tab becomes visible so stale data recovers instantly.
        startPolling();

        // Subscribe to visibility changes for pause/resume. The handler
        // is stored so unmount can remove it cleanly.
        visibilityHandler = handleVisibilityChange;
        document.addEventListener('visibilitychange', visibilityHandler);

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
        stopPolling();
        if (chartInstance) {
            chartInstance.destroy();
            chartInstance = null;
        }
        if (themeChangeHandler) {
            window.removeEventListener('themechange', themeChangeHandler);
            themeChangeHandler = null;
        }
        if (visibilityHandler) {
            document.removeEventListener('visibilitychange', visibilityHandler);
            visibilityHandler = null;
        }
        state.consecutiveFailures = 0;
    },
};
