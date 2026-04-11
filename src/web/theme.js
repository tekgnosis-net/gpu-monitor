/*
 * theme.js — Dark/light/auto theming with OS preference + user override.
 *
 * Phase 4. Three modes:
 *   'auto'  — follow prefers-color-scheme (default)
 *   'light' — force light theme
 *   'dark'  — force dark theme
 *
 * The mode persists in localStorage under the key 'gpu-monitor:theme'.
 * When 'auto' is active, a matchMedia listener flips the theme live if
 * the user toggles their OS theme — no reload required.
 *
 * Tokens.css does most of the heavy lifting via the data-theme attribute
 * on <html>; this module just keeps that attribute in sync with the
 * user's choice.
 */

const STORAGE_KEY = 'gpu-monitor:theme';
const VALID_MODES = new Set(['auto', 'light', 'dark']);

let currentMode = 'auto';
let mediaQuery = null;

function resolveMode(mode) {
    if (mode === 'light') return 'light';
    if (mode === 'dark') return 'dark';
    // 'auto': follow OS preference
    if (window.matchMedia('(prefers-color-scheme: dark)').matches) {
        return 'dark';
    }
    return 'light';
}

function applyMode(mode) {
    const resolved = resolveMode(mode);
    const root = document.documentElement;
    if (resolved === 'dark') {
        root.setAttribute('data-theme', 'dark');
    } else if (resolved === 'light') {
        root.setAttribute('data-theme', 'light');
    }
    // Emit a custom event so views can react if they need to re-render
    // (e.g. Chart.js re-fetching axis colors from the computed CSS).
    window.dispatchEvent(new CustomEvent('themechange', { detail: { mode, resolved } }));
}

function loadStoredMode() {
    try {
        const stored = localStorage.getItem(STORAGE_KEY);
        return VALID_MODES.has(stored) ? stored : 'auto';
    } catch {
        return 'auto';
    }
}

function saveMode(mode) {
    try {
        localStorage.setItem(STORAGE_KEY, mode);
    } catch {
        // localStorage may fail in private browsing / incognito; that's
        // fine, the mode just won't persist across reloads.
    }
}

/* ─── Public API ────────────────────────────────────────────────────────── */

export function initTheme() {
    currentMode = loadStoredMode();
    applyMode(currentMode);

    // Subscribe to OS preference changes so 'auto' mode tracks live.
    mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    mediaQuery.addEventListener('change', () => {
        if (currentMode === 'auto') {
            applyMode(currentMode);
        }
    });
}

export function getTheme() {
    return currentMode;
}

export function setTheme(mode) {
    if (!VALID_MODES.has(mode)) {
        console.warn(`theme: invalid mode "${mode}", ignoring`);
        return;
    }
    currentMode = mode;
    saveMode(mode);
    applyMode(mode);
}

export function cycleTheme() {
    // auto → light → dark → auto
    const next = { auto: 'light', light: 'dark', dark: 'auto' }[currentMode];
    setTheme(next);
    return next;
}
