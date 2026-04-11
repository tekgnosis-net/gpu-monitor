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

// Apply (or remove) the dark-mode invert filter on the preview
// iframe. The email template is intentionally hardcoded to light-
// mode colors because its primary consumer is a mail client, and
// mail clients predominantly ignore prefers-color-scheme. Inside
// the dashboard iframe embed, that produces a jarring bright-white
// block against a dark UI. Inverting via CSS filter is a one-line
// bridge that keeps the server-side render authentic (WYSIWYG for
// email recipients) while giving a dark-friendly appearance
// when embedded in a dark dashboard.
//
// The invert() + hue-rotate(180deg) combo preserves the subjective
// lightness of any colored regions while flipping the polarity of
// grays — it's the standard CSS technique for "cheap dark mode".
// It works here because the preview endpoint sets include_charts=
// False, so there are no PNG images to corrupt with inversion.
// If charts are ever added to the preview, this approach would
// need to change.
function applyPreviewTheme(iframe) {
    if (!iframe) return;
    const theme = resolvedTheme();
    if (theme === 'dark') {
        iframe.style.filter = 'invert(1) hue-rotate(180deg)';
    } else {
        iframe.style.filter = '';
    }
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
            const iframe = document.getElementById('report-preview-iframe');
            if (iframe) {
                iframe.src = api.getReportPreviewUrl(template.id);
                // Re-apply the theme filter after src change so the
                // new preview content honors the current dashboard
                // theme from its first paint rather than flashing
                // light and then inverting.
                applyPreviewTheme(iframe);
            }
        }));

        // Iframe for the live HTML preview.
        //
        // Background is set to match the email template's body
        // color (#f5f5f7) rather than pure white. When the dashboard
        // is in light mode, the iframe appears identical to how
        // Gmail/Outlook/Apple Mail will render the email. When the
        // dashboard is in dark mode, applyPreviewTheme() applies a
        // CSS invert+hue-rotate filter that bridges the contrast
        // without changing the server-rendered HTML — the email
        // template's "light mode by default" contract stays intact
        // for real mail-client consumers.
        const iframe = document.createElement('iframe');
        iframe.id = 'report-preview-iframe';
        iframe.src = api.getReportPreviewUrl(state.template);
        iframe.style.width = '100%';
        iframe.style.height = '600px';
        iframe.style.border = 'none';
        iframe.style.borderRadius = 'var(--radius-md)';
        iframe.style.background = '#f5f5f7';
        iframe.setAttribute('sandbox', 'allow-same-origin');
        iframe.setAttribute('title', 'Report preview');
        applyPreviewTheme(iframe);

        container.append(buildPreviewCard(iframe));
        container.append(buildScheduleList());

        // Subscribe to theme changes so toggling the sidebar theme
        // updates the iframe filter in place without requiring a
        // page reload or a view re-mount. theme.js emits the
        // 'themechange' window event on every initTheme call and
        // every cycleTheme call (Phase 4).
        themeChangeHandler = () => {
            const liveIframe = document.getElementById('report-preview-iframe');
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
