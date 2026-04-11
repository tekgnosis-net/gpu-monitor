/*
 * views/report.js — Report view.
 *
 * Phase 6c. Shows a live HTML preview of what the email report will
 * look like, plus a list of configured schedules with Run Now buttons.
 *
 * Design choices:
 *
 *   1. The preview is an <iframe> with src pointing at
 *      /api/reports/preview?template=daily. We use src (not srcdoc)
 *      so the browser shows a genuine loading state while the server
 *      renders the HTML, and so the iframe's scroll position persists
 *      across template changes.
 *
 *   2. The iframe is sandboxed to "allow-same-origin" only — no
 *      scripts, no forms, no top-navigation. Paranoid defense even
 *      though the rendered HTML comes from our own Jinja template
 *      reading our own data; content-injection gadgets are cheap to
 *      prevent at the frame boundary.
 *
 *   3. Template switcher is a simple button group (Daily / Weekly /
 *      Monthly) above the iframe, mirroring the Power view's
 *      window picker pattern.
 *
 *   4. The schedule list is a read-only mirror of what the Settings →
 *      Reports tab already renders. We don't duplicate the add/edit/
 *      remove UI here — if the user wants to edit a schedule they
 *      click through to Settings. This view's job is "preview and
 *      fire", not "configure".
 */

import * as api from '../api.js';

const TEMPLATES = [
    { id: 'daily',   label: 'Daily',   window: '24 hours' },
    { id: 'weekly',  label: 'Weekly',  window: '7 days' },
    { id: 'monthly', label: 'Monthly', window: '30 days' },
];

let state = {
    template: 'daily',
    schedules: [],
};

// Detach handle for the themechange listener, cleared on unmount
// so the listener doesn't leak across view re-entries.
let themeChangeHandler = null;

// Resolve the effective theme of the DOCUMENT (what the user
// visually experiences), not the raw data-theme attribute. Phase 4's
// theme.js sets `data-theme="light"` / `data-theme="dark"` explicitly,
// but also supports an auto mode where the attribute may be absent
// or equal to "auto" — in that case the OS preference decides via
// the prefers-color-scheme media query.
//
// Returns 'dark' or 'light'.
function resolvedTheme() {
    const attr = document.documentElement.getAttribute('data-theme');
    if (attr === 'dark') return 'dark';
    if (attr === 'light') return 'light';
    // auto / missing → defer to OS preference
    const mql = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');
    return mql && mql.matches ? 'dark' : 'light';
}

// Reload the preview iframe with the correct theme query param
// so the server returns a dark-mode-override'd HTML body when the
// dashboard is dark, and the default light HTML when the dashboard
// is light.
//
// An earlier implementation used a client-side CSS `filter:
// invert(1) hue-rotate(180deg)` on the iframe element, which was
// cheap but had a fundamental limitation: CSS inversion preserves
// the relative brightness ordering of elements. In the email
// template, cards are brighter than body (a standard "elevated
// surface" pattern), which after inversion makes cards DARKER than
// body — reversing the visual-depth convention that dark-mode UX
// relies on. The result was cards dissolving into the background
// because they were on the wrong side of the body's brightness.
//
// The correct fix is server-side: when `?theme=dark` is in the
// query string, render.py appends a dark-mode `<style>` block
// that maps specific element classes to proper dark-palette
// colors (body #1c1c1e, elevated cards #2c2c2e, deepest tiles
// #3a3a3c) — with cards LIGHTER than body, restoring the
// depth hierarchy. The email-send path still uses the default
// light template so real recipients see the intended design.
//
// The reload is smoothed by a short opacity fade: zero out the
// opacity, change the src, wait for the `load` event on the
// iframe, fade back in. The total transition is fast (~200 ms)
// because preview HTML is small and local, but the fade prevents
// the white-flash-then-dark-repaint that raw src reassignment
// would cause in some browsers.
function applyPreviewTheme(iframe) {
    if (!iframe) return;
    const theme = resolvedTheme();
    const newSrc = api.getReportPreviewUrl(
        state.template,
        theme === 'dark' ? 'dark' : null,
    );
    // Strip the query string off both sides and compare the path
    // + theme tag so we don't trigger reloads when the theme didn't
    // actually change (e.g. mount() calls this once and the src is
    // already right).
    if (iframe.src === newSrc) return;

    const onLoad = () => {
        iframe.style.opacity = '1';
        iframe.removeEventListener('load', onLoad);
    };
    iframe.addEventListener('load', onLoad);

    iframe.style.transition = 'opacity var(--motion-fast, 150ms linear)';
    iframe.style.opacity = '0';
    // Give the browser a frame to apply the opacity=0 paint before
    // we change src, so the fade-out is visible rather than instant.
    requestAnimationFrame(() => {
        iframe.src = newSrc;
    });
}

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function buildHeader() {
    const header = el('header');
    header.append(el('h1', null, 'Reports'));
    header.append(el('div', 'subtitle',
        'Preview scheduled email reports and trigger them manually'));
    return header;
}

function buildTemplatePicker(onChange) {
    const wrap = el('section');

    const label = el('div', null, 'Template');
    label.style.marginBottom = 'var(--space-2)';
    label.style.color = 'var(--text-tertiary)';
    label.style.fontSize = 'var(--font-size-sm)';

    const picker = el('div', 'time-range');
    TEMPLATES.forEach(t => {
        const btn = el('button', null, t.label);
        btn.type = 'button';
        btn.setAttribute('data-template', t.id);
        if (t.id === state.template) btn.setAttribute('aria-current', 'true');
        btn.addEventListener('click', () => {
            state.template = t.id;
            picker.querySelectorAll('button').forEach(b => {
                if (b.getAttribute('data-template') === t.id) {
                    b.setAttribute('aria-current', 'true');
                } else {
                    b.removeAttribute('aria-current');
                }
            });
            onChange(t);
        });
        picker.append(btn);
    });

    wrap.append(label, picker);
    return wrap;
}

function buildPreviewCard(iframe) {
    const card = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Live preview'));
    header.append(el('div', 'subtitle',
        'Rendered from the current settings + database. Charts and images are hidden in the preview to keep it snappy.'));
    card.append(header);
    card.append(iframe);
    return card;
}

function buildScheduleList() {
    const card = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Scheduled reports'));
    header.append(el('div', 'subtitle',
        'Configured schedules from Settings → Reports. Use "Run now" to fire one immediately.'));
    card.append(header);

    if (state.schedules.length === 0) {
        const empty = el('div', 'empty-state');
        empty.style.padding = 'var(--space-4) var(--space-2)';
        const icon = el('div', 'icon', '📄');
        const title = el('div', 'title', 'No schedules configured');
        const desc = el('div', 'description',
            'Add a schedule in Settings → Reports to see it here. Each schedule needs a unique ID, template type, cron expression, and at least one recipient.');
        empty.append(icon, title, desc);
        card.append(empty);
        return card;
    }

    const list = el('div');
    list.style.display = 'flex';
    list.style.flexDirection = 'column';
    list.style.gap = 'var(--space-3)';

    state.schedules.forEach(schedule => {
        list.append(buildScheduleRow(schedule));
    });

    card.append(list);
    return card;
}

function buildScheduleRow(schedule) {
    const row = el('div');
    row.style.padding = 'var(--space-3) var(--space-4)';
    row.style.border = '1px solid var(--border-subtle)';
    row.style.borderRadius = 'var(--radius-md)';

    const topRow = el('div');
    topRow.style.display = 'flex';
    topRow.style.justifyContent = 'space-between';
    topRow.style.alignItems = 'baseline';
    topRow.style.gap = 'var(--space-3)';

    const left = el('div');
    const name = el('div', null, schedule.id);
    name.style.fontWeight = 'var(--font-weight-semibold)';
    const meta = el('div');
    meta.style.fontSize = 'var(--font-size-sm)';
    meta.style.color = 'var(--text-tertiary)';
    meta.style.fontFamily = 'var(--font-mono)';
    meta.textContent = `${schedule.template} · ${schedule.cron}`;
    left.append(name, meta);

    const runBtn = el('button', 'small', 'Run now');
    runBtn.type = 'button';
    const runStatus = el('div');
    runStatus.style.fontSize = 'var(--font-size-sm)';
    runStatus.style.marginTop = 'var(--space-1)';

    runBtn.addEventListener('click', async () => {
        runBtn.disabled = true;
        runBtn.textContent = 'Sending…';
        runStatus.textContent = '';
        try {
            const result = await api.runScheduleNow(schedule.id);
            runStatus.textContent =
                'Sent at ' + new Date(result.last_run_epoch * 1000).toLocaleString();
            runStatus.style.color = 'var(--success)';
        } catch (err) {
            runStatus.textContent = 'Failed: ' + err.message;
            runStatus.style.color = 'var(--danger)';
        } finally {
            runBtn.disabled = false;
            runBtn.textContent = 'Run now';
        }
    });

    topRow.append(left, runBtn);
    row.append(topRow);

    const recipients = el('div');
    recipients.style.fontSize = 'var(--font-size-sm)';
    recipients.style.color = 'var(--text-secondary)';
    recipients.style.marginTop = 'var(--space-2)';
    recipients.textContent = 'To: ' + (schedule.recipients || []).join(', ');
    row.append(recipients);

    if (schedule.last_run_epoch) {
        const last = el('div');
        last.style.fontSize = 'var(--font-size-sm)';
        last.style.color = 'var(--text-tertiary)';
        last.textContent =
            'Last run: ' + new Date(schedule.last_run_epoch * 1000).toLocaleString();
        row.append(last);
    }

    row.append(runStatus);
    return row;
}

export const reportView = {
    name: 'report',

    async mount(container) {
        container.append(buildHeader());

        // Fetch initial settings to populate the schedule list
        try {
            const settings = await api.getSettings();
            state.schedules = Array.isArray(settings.schedules) ? settings.schedules : [];
        } catch {
            state.schedules = [];
        }

        container.append(buildTemplatePicker((template) => {
            state.template = template.id;
            const iframe = document.getElementById('report-preview-iframe');
            applyPreviewTheme(iframe);
        }));

        // Iframe for the live HTML preview.
        //
        // The initial src is built with the current theme baked in so
        // there's no "light-mode flash, then reload to dark" sequence
        // on view mount. applyPreviewTheme()'s early-return short-circuits
        // when the src already matches the resolved theme, so the fade
        // reload path only fires when the user actually changes themes
        // or switches templates.
        //
        // The iframe's own background matches the theme for the ~100ms
        // window before the loaded HTML paints — dark body under light
        // iframe background would flash white on every reload, which is
        // the exact "white-flash-of-death" that theme-aware sites fight
        // on page load.
        const initialTheme = resolvedTheme();
        const iframe = document.createElement('iframe');
        iframe.id = 'report-preview-iframe';
        iframe.src = api.getReportPreviewUrl(
            state.template,
            initialTheme === 'dark' ? 'dark' : null,
        );
        iframe.style.width = '100%';
        iframe.style.height = '600px';
        iframe.style.border = 'none';
        iframe.style.borderRadius = 'var(--radius-md)';
        iframe.style.background = initialTheme === 'dark' ? '#1c1c1e' : '#f5f5f7';
        iframe.setAttribute('sandbox', 'allow-same-origin');
        iframe.setAttribute('title', 'Report preview');

        container.append(buildPreviewCard(iframe));
        container.append(buildScheduleList());

        // Subscribe to theme changes so toggling the sidebar theme
        // reloads the iframe with the correct server-rendered variant
        // (light or dark) without requiring a page reload or a view
        // re-mount. theme.js emits the 'themechange' window event on
        // every initTheme call and every cycleTheme call (Phase 4).
        themeChangeHandler = () => {
            const liveIframe = document.getElementById('report-preview-iframe');
            if (!liveIframe) return;
            // Keep the iframe's own background in sync with the theme
            // so the brief blank between src change and the loaded
            // HTML's first paint doesn't flash the opposite color.
            const theme = resolvedTheme();
            liveIframe.style.background = theme === 'dark' ? '#1c1c1e' : '#f5f5f7';
            applyPreviewTheme(liveIframe);
        };
        window.addEventListener('themechange', themeChangeHandler);

        // Also listen for OS-level prefers-color-scheme changes
        // when the user is in 'auto' theme mode and flips their
        // OS appearance. theme.js already listens to matchMedia
        // and emits themechange when the OS flips, so this is
        // covered by the themechange subscription above. No
        // separate matchMedia listener needed here.
    },

    unmount() {
        state.schedules = [];
        if (themeChangeHandler) {
            window.removeEventListener('themechange', themeChangeHandler);
            themeChangeHandler = null;
        }
    },
};
