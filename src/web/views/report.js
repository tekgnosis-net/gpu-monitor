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
            if (iframe) iframe.src = api.getReportPreviewUrl(template.id);
        }));

        // Iframe for the live HTML preview
        const iframe = document.createElement('iframe');
        iframe.id = 'report-preview-iframe';
        iframe.src = api.getReportPreviewUrl(state.template);
        iframe.style.width = '100%';
        iframe.style.height = '600px';
        iframe.style.border = 'none';
        iframe.style.borderRadius = 'var(--radius-md)';
        iframe.style.background = 'white';
        iframe.setAttribute('sandbox', 'allow-same-origin');
        iframe.setAttribute('title', 'Report preview');

        container.append(buildPreviewCard(iframe));
        container.append(buildScheduleList());
    },

    unmount() {
        state.schedules = [];
    },
};
