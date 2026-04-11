/*
 * alerts.js — Dashboard alert state machine.
 *
 * Phase 7. Re-wires the alert system that Phase 4 stripped out when
 * the legacy gpu-stats.html was rewritten. Reads thresholds from
 * /api/settings.alerts once at dashboard mount (plus a refresh on
 * the "settingschange" window event, if we ever emit one), compares
 * each current-metrics poll result against them, and fires a toast
 * banner + optional sound + optional browser Notification on breach.
 *
 * State machine per (gpu_index, metric):
 *
 *   idle  → over-threshold → fire → cooldown → idle
 *
 * A metric that's been over-threshold for < cooldown_seconds since
 * its last fire stays in the cooldown state — we don't spam the
 * user with toasts every 4 seconds. Once the metric drops back
 * under the threshold we transition to idle and the next breach
 * will fire immediately (no lingering cooldown).
 *
 * Toast rendering uses createElement + textContent (no innerHTML
 * with user-controlled data — the message contains GPU name from
 * the inventory, which is user-trusted but still sanitized
 * defensively).
 *
 * Audio playback is gated on `sound_enabled` in settings + a
 * user-interaction requirement (the browser refuses autoplay
 * before any click on the page). We attempt play() and silently
 * swallow NotAllowedError — no console spam on first-load polls.
 *
 * Browser Notification API is gated on `notifications_enabled` in
 * settings + the user's explicit permission grant. We request
 * permission ONLY when the user flips the toggle in the Settings
 * view — never on page load — to avoid the "spooky unprompted
 * permission request" UX smell that Phase 4's hardcoded-to-
 * configurable audit specifically called out.
 */

import * as api from './api.js';

const METRICS = [
    { key: 'temperature', label: 'Temperature', unit: '°C', thresholdKey: 'temperature_c' },
    { key: 'utilization', label: 'Utilization', unit: '%',  thresholdKey: 'utilization_pct' },
    { key: 'power',       label: 'Power',       unit: 'W',  thresholdKey: 'power_w' },
];

let thresholds = {
    temperature_c: 80,
    utilization_pct: 100,
    power_w: 300,
    cooldown_seconds: 10,
    sound_enabled: true,
    notifications_enabled: false,
};

// State per (gpu_index, metric) → last fire epoch. Keys look like
// "0:temperature". Missing key → idle, ready to fire immediately.
const lastFireEpoch = new Map();

// Cached <audio> element for the alert sound. Created lazily on
// first fire so a dashboard that never triggers an alert doesn't
// preload an audio file.
let alertAudio = null;

// Cached toast container element. Appended to <body> on first
// fire; reused across all subsequent fires. Cleared by the view's
// unmount if present, though in practice it persists for the page
// lifetime.
let toastContainer = null;


/* ─── Settings refresh ──────────────────────────────────────────────────── */

export async function loadThresholdsFromServer() {
    // Fetches /api/settings and reads the `alerts` section.
    // Called once on dashboard mount. If the fetch fails, the
    // module-level defaults stay in place — failing closed on
    // alert thresholds means temperature=80 / utilization=100 /
    // power=300, which matches the pre-Phase-4 hardcoded values
    // from the inventory, so existing behavior is preserved on
    // any API failure.
    try {
        const settings = await api.getSettings();
        const alerts = settings.alerts || {};
        thresholds = {
            temperature_c: Number(alerts.temperature_c ?? 80),
            utilization_pct: Number(alerts.utilization_pct ?? 100),
            power_w: Number(alerts.power_w ?? 300),
            cooldown_seconds: Number(alerts.cooldown_seconds ?? 10),
            sound_enabled: Boolean(alerts.sound_enabled ?? true),
            notifications_enabled: Boolean(alerts.notifications_enabled ?? false),
        };
    } catch (err) {
        console.warn('alerts: could not load thresholds from /api/settings:', err);
    }
}

/* ─── Main check — called once per poll with the latest metrics ─────────── */

export function checkMetrics(currentMetrics, gpuInventory) {
    // currentMetrics: array of per-GPU metric objects from
    //   /api/metrics/current (gpu_index, temperature, utilization,
    //   memory, power)
    // gpuInventory: array of inventory entries from /api/gpus
    //   (used to look up the friendly GPU name for the toast)
    //
    // For each (gpu, metric) pair: if the current value is >
    // threshold AND we're not in cooldown, fire the alert.
    const now = Date.now() / 1000;

    for (const gpuMetrics of currentMetrics) {
        const gpuIndex = gpuMetrics.gpu_index;
        const gpu = gpuInventory.find(g => g.index === gpuIndex) || {};
        const gpuName = gpu.name || `GPU ${gpuIndex}`;

        for (const metric of METRICS) {
            const value = Number(gpuMetrics[metric.key]);
            if (!Number.isFinite(value)) continue;

            const threshold = thresholds[metric.thresholdKey];
            if (!Number.isFinite(threshold) || threshold <= 0) continue;

            const stateKey = `${gpuIndex}:${metric.key}`;

            if (value > threshold) {
                // In cooldown? Silently skip.
                const lastFire = lastFireEpoch.get(stateKey) || 0;
                if (now - lastFire < thresholds.cooldown_seconds) continue;

                // Fire!
                lastFireEpoch.set(stateKey, now);
                fireAlert({ gpuName, metric, value, threshold });
            } else {
                // Below threshold — clear the cooldown so the next
                // breach fires immediately. Without this, a metric
                // that crosses back and forth would only fire once
                // per cooldown window even though the user wants
                // to know about each crossing.
                if (lastFireEpoch.has(stateKey)) {
                    // Only clear if the value has been below
                    // threshold for at least one poll — flapping
                    // noise right at the boundary shouldn't
                    // trigger instant re-fire.
                    const lastFire = lastFireEpoch.get(stateKey);
                    if (now - lastFire > 2) {
                        lastFireEpoch.delete(stateKey);
                    }
                }
            }
        }
    }
}

/* ─── Fire paths ────────────────────────────────────────────────────────── */

function fireAlert({ gpuName, metric, value, threshold }) {
    const message = `${gpuName}: ${metric.label} is ${value.toFixed(1)}${metric.unit} (threshold ${threshold}${metric.unit})`;

    // Toast always fires — users who want no visual alerts should
    // set very high thresholds in Settings → Alerts.
    showToast(message, metric.key);

    if (thresholds.sound_enabled) {
        playAlertSound();
    }

    if (thresholds.notifications_enabled) {
        fireBrowserNotification(message);
    }
}

/* ─── Toast rendering ──────────────────────────────────────────────────── */

function ensureToastContainer() {
    if (toastContainer && document.body.contains(toastContainer)) {
        return toastContainer;
    }
    toastContainer = document.createElement('div');
    toastContainer.id = 'alert-toast-container';
    toastContainer.setAttribute('role', 'status');
    toastContainer.setAttribute('aria-live', 'polite');
    toastContainer.setAttribute('aria-atomic', 'true');
    // Stack toasts top-right with a small gap
    toastContainer.style.position = 'fixed';
    toastContainer.style.top = 'var(--space-4, 16px)';
    toastContainer.style.right = 'var(--space-4, 16px)';
    toastContainer.style.zIndex = '1000';
    toastContainer.style.display = 'flex';
    toastContainer.style.flexDirection = 'column';
    toastContainer.style.gap = 'var(--space-2, 8px)';
    toastContainer.style.pointerEvents = 'none';
    document.body.append(toastContainer);
    return toastContainer;
}

function showToast(message, metricKey) {
    const container = ensureToastContainer();

    const toast = document.createElement('div');
    toast.className = 'alert-toast';
    // Inline styles so the toast renders correctly even if
    // components.css hasn't added a rule for it (avoiding a
    // required stylesheet change for Phase 7). Future polish
    // could promote these to a shared class.
    toast.style.background = 'var(--bg-secondary, #fff)';
    toast.style.color = 'var(--text-primary, #1d1d1f)';
    toast.style.border = '1px solid var(--border-regular, rgba(60,60,67,0.18))';
    toast.style.borderLeft = '4px solid var(--danger, #ff3b30)';
    toast.style.borderRadius = 'var(--radius-md, 10px)';
    toast.style.boxShadow = 'var(--shadow-md, 0 2px 8px rgba(0,0,0,0.12))';
    toast.style.padding = 'var(--space-3, 12px) var(--space-4, 16px)';
    toast.style.fontSize = 'var(--font-size-sm, 13px)';
    toast.style.minWidth = '280px';
    toast.style.maxWidth = '400px';
    toast.style.pointerEvents = 'auto';
    // Fade-in via the motion token — respects reduced-motion
    toast.style.opacity = '0';
    toast.style.transition = 'opacity var(--motion-fast, 150ms linear)';

    // Safe DOM — no innerHTML with metric data
    const label = document.createElement('span');
    label.style.fontWeight = 'var(--font-weight-semibold, 600)';
    label.textContent = 'Alert — ';
    const text = document.createElement('span');
    text.textContent = message;
    toast.append(label, text);

    container.append(toast);

    // Trigger the fade-in after one paint
    requestAnimationFrame(() => {
        toast.style.opacity = '1';
    });

    // Auto-dismiss after 5 seconds
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => {
            if (toast.parentNode) toast.parentNode.removeChild(toast);
        }, 200);
    }, 5000);
}

/* ─── Sound playback ───────────────────────────────────────────────────── */

function playAlertSound() {
    // Lazily create the audio element on first fire. The sound
    // file lives at /sounds/alert.mp3 (preserved from the
    // legacy frontend's sounds/ directory).
    if (!alertAudio) {
        alertAudio = new Audio('/sounds/alert.mp3');
        alertAudio.volume = 0.5;
    }
    // .play() returns a Promise that rejects with NotAllowedError
    // if the user hasn't interacted with the page yet. Silently
    // swallow so we don't spam the console on page load.
    alertAudio.currentTime = 0;
    const p = alertAudio.play();
    if (p && typeof p.catch === 'function') {
        p.catch(() => { /* autoplay gated — user must click first */ });
    }
}

/* ─── Browser notifications ────────────────────────────────────────────── */

function fireBrowserNotification(message) {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission !== 'granted') return;
    try {
        new Notification('GPU Monitor', { body: message });
    } catch (err) {
        console.warn('alerts: notification failed:', err);
    }
}

// Exported so the Settings view's "Desktop notifications" checkbox
// can call it when the user explicitly flips the toggle ON. Per
// the Phase 4 hardcoded-to-configurable audit, permission must
// NEVER be requested on page load — only in direct response to
// a user opt-in action.
export async function requestNotificationPermission() {
    if (typeof Notification === 'undefined') return 'unsupported';
    if (Notification.permission === 'granted') return 'granted';
    if (Notification.permission === 'denied') return 'denied';
    try {
        const result = await Notification.requestPermission();
        return result;
    } catch (err) {
        console.warn('alerts: permission request failed:', err);
        return 'error';
    }
}
