/*
 * api.js — Small wrapper around the /api/* endpoints.
 *
 * Phase 4 of the v1.0.0 overhaul. Centralizes the fetch + error-handling
 * + shape-normalization logic that was scattered inline across the
 * retrofitted gpu-stats.html in Phase 3. Every view module imports from
 * this module instead of calling fetch() directly, so if the API surface
 * changes (e.g. Phase 5 adds /api/stats/power, Phase 6 adds /api/settings)
 * there is exactly one place to update.
 *
 * Design principles:
 *   - Every function returns a Promise that resolves to a sensible default
 *     on failure, NEVER rejects. Views render an empty state rather than
 *     showing an unhandled rejection in the console.
 *   - Defaults for list endpoints are [], for scalar endpoints are {} or
 *     primitive zero values. Never null or undefined.
 *   - No retries, no caching: if the view needs retry-on-failure or
 *     revalidation, that's the view's call. Keeping this layer thin
 *     makes it easy to reason about.
 */

const API_BASE = '/api';

async function _fetchJson(path, fallback) {
    try {
        const response = await fetch(`${API_BASE}${path}`);
        if (!response.ok) {
            console.warn(`api: ${path} returned ${response.status}`);
            return fallback;
        }
        return await response.json();
    } catch (error) {
        console.warn(`api: ${path} fetch failed:`, error);
        return fallback;
    }
}

/* ─── Endpoint helpers ─────────────────────────────────────────────────── */

export async function getHealth() {
    return _fetchJson('/health', { ok: false, version: 'unknown', schema: 0 });
}

export async function getVersion() {
    const data = await _fetchJson('/version', { version: 'unknown' });
    return data.version || 'unknown';
}

export async function getGpus() {
    const data = await _fetchJson('/gpus', { gpus: [] });
    return Array.isArray(data.gpus) ? data.gpus : [];
}

export async function getCurrentMetrics() {
    const data = await _fetchJson('/metrics/current', []);
    return Array.isArray(data) ? data : [];
}

export async function getHistory(range = '24h', gpuIndex = 0) {
    const params = new URLSearchParams({ range, gpu: String(gpuIndex) });
    const fallback = {
        timestamps: [], temperatures: [], utilizations: [],
        memory: [], power: [],
    };
    const data = await _fetchJson(`/metrics/history?${params}`, fallback);
    return {
        timestamps:   Array.isArray(data.timestamps)   ? data.timestamps   : [],
        temperatures: Array.isArray(data.temperatures) ? data.temperatures : [],
        utilizations: Array.isArray(data.utilizations) ? data.utilizations : [],
        memory:       Array.isArray(data.memory)       ? data.memory       : [],
        power:        Array.isArray(data.power)        ? data.power        : [],
    };
}

export async function getStats24h() {
    const data = await _fetchJson('/stats/24h', []);
    return Array.isArray(data) ? data : [];
}
