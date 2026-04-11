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

/* ─── Phase 6 — Settings, housekeeping, schedules ──────────────────────── */

export async function getSettings() {
    // Phase 6a: returns the full settings tree with smtp.password_enc
    // replaced by a boolean smtp.password_set. Never null — the server
    // falls back to DEFAULT_SETTINGS on missing file.
    return _fetchJson('/settings', {});
}

async function _putJson(path, body) {
    // Mutating helper: PUT a JSON body and return the parsed response.
    // Unlike _fetchJson which swallows errors into a fallback, mutating
    // calls re-raise so the Settings view can show a toast on failure.
    try {
        const response = await fetch(`${API_BASE}${path}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || `HTTP ${response.status}`);
            err.status = response.status;
            err.detail = data.detail;
            throw err;
        }
        return data;
    } catch (error) {
        if (error.status) throw error;
        // Network / JSON parse failure — surface as a consistent shape
        const err = new Error(error.message || 'network error');
        err.status = 0;
        throw err;
    }
}

async function _postJson(path, body = undefined) {
    try {
        const init = {
            method: 'POST',
            headers: body ? { 'Content-Type': 'application/json' } : {},
        };
        if (body !== undefined) init.body = JSON.stringify(body);
        const response = await fetch(`${API_BASE}${path}`, init);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const err = new Error(data.error || `HTTP ${response.status}`);
            err.status = response.status;
            err.detail = data.detail;
            throw err;
        }
        return data;
    } catch (error) {
        if (error.status) throw error;
        const err = new Error(error.message || 'network error');
        err.status = 0;
        throw err;
    }
}

export async function putSettings(partial) {
    // Partial-merge update — only send the fields you want to change.
    // The server deep-merges over the current settings.json and
    // validates via Pydantic before writing. Throws on 4xx / 5xx so
    // the view can surface the server's error message inline.
    return _putJson('/settings', partial);
}

export async function testSmtp(to = null) {
    // Trigger a real SMTP test send. `to` optional — defaults to the
    // configured user address if omitted. Throws on 4xx / 5xx with
    // the server's error message so the Settings view can show it
    // without a generic "network error" fallback.
    return _postJson('/settings/smtp/test', to ? { to } : undefined);
}

export async function runScheduleNow(scheduleId) {
    // Synchronously fires one schedule via the server's run-now
    // endpoint. Blocks until render + send complete — the UI should
    // show a spinner because this can take 5-15 seconds on a slow
    // SMTP relay.
    const encoded = encodeURIComponent(scheduleId);
    return _postJson(`/schedules/${encoded}/run-now`);
}

export async function getDbInfo() {
    // Housekeeping tab: current DB size, row count, oldest/newest,
    // per-GPU breakdown.
    return _fetchJson('/housekeeping/db-info', {
        size_bytes: 0,
        row_count: 0,
        oldest_epoch: null,
        newest_epoch: null,
        row_count_per_gpu: [],
    });
}

export async function vacuumDb() {
    // Triggers a blocking VACUUM. Can take seconds on a large DB.
    return _postJson('/housekeeping/vacuum');
}

export async function purgeOldData(days) {
    // DELETE rows older than N days. Idempotent.
    return _postJson('/housekeeping/purge', { days });
}

export async function getReportPreviewUrl(template = 'daily') {
    // Returns the URL of the HTML preview endpoint for use as an
    // iframe src (not srcdoc — we want a real HTTP fetch so the
    // iframe can cache and the browser can show a loading state).
    // This is a synchronous helper, not a fetch, so the view can
    // set iframe.src directly without an await.
    const params = new URLSearchParams({ template });
    return `${API_BASE}/reports/preview?${params}`;
}

/* ─── Phase 5 (existing) ──────────────────────────────────────────────── */

export async function getPowerStats(range = '24h', gpuIndex = 0) {
    // Phase 5: integrated energy + peak / avg / sample counts for one GPU
    // over a selectable window. The fallback shape matches the server
    // handler's success shape so the view never has to null-check
    // individual fields — it only has to check `insufficient_telemetry`.
    const params = new URLSearchParams({ range, gpu: String(gpuIndex) });
    const fallback = {
        range,
        gpu_index: gpuIndex,
        energy_wh: 0,
        peak_power_w: 0,
        avg_power_w: 0,
        samples_total: 0,
        samples_invalid: 0,
        insufficient_telemetry: true,
    };
    const data = await _fetchJson(`/stats/power?${params}`, fallback);
    // Guard against a partial upstream response — treat missing numeric
    // keys as zero rather than propagating undefined into chart /
    // formatting code. Safety flag rule: a missing `insufficient_telemetry`
    // defaults to TRUE, not false. The flag exists to warn the user, and
    // silently treating an incomplete response as "telemetry OK" would
    // give false confidence. Only an *explicit* false from the server
    // should produce the "sufficient" state.
    const hasTelemetryFlag = Object.prototype.hasOwnProperty
        .call(data, 'insufficient_telemetry');
    return {
        range:        data.range ?? range,
        gpu_index:    Number.isFinite(data.gpu_index) ? data.gpu_index : gpuIndex,
        energy_wh:    Number.isFinite(data.energy_wh)    ? data.energy_wh    : 0,
        peak_power_w: Number.isFinite(data.peak_power_w) ? data.peak_power_w : 0,
        avg_power_w:  Number.isFinite(data.avg_power_w)  ? data.avg_power_w  : 0,
        samples_total:   Number.isFinite(data.samples_total)   ? data.samples_total   : 0,
        samples_invalid: Number.isFinite(data.samples_invalid) ? data.samples_invalid : 0,
        insufficient_telemetry: hasTelemetryFlag
            ? Boolean(data.insufficient_telemetry)
            : true,
    };
}
