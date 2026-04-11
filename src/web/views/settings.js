/*
 * views/settings.js — Multi-tab settings view.
 *
 * Phase 6c. Implements the tabbed configuration form for:
 *   Collection / SMTP / Alerts / Power / Housekeeping / Logging / Reports / Theme
 *
 * Design choices:
 *
 *   1. No Lit for the form itself. Lit is great for reusable components
 *      (gauge, gpu-card, info-tip) but a one-off 8-tab settings form
 *      with mostly plain <input> elements doesn't benefit from it —
 *      the reactive rendering + shadow-DOM overhead outweighs the
 *      markup savings. We use plain createElement + textContent
 *      following the pattern established in dashboard.js / power.js.
 *
 *   2. One API call per tab save, not one per field. Each tab has a
 *      Save button that does a partial PUT of just that tab's
 *      subsection, which composes cleanly with the server's deep-merge
 *      logic. Field-level auto-save would thrash the file on every
 *      keystroke and make error recovery painful.
 *
 *   3. Optimistic client-side validation via HTML5 input attributes
 *      (min/max/step/required), authoritative server-side validation
 *      via Pydantic. A PUT with out-of-range values returns 400 with
 *      field detail; we surface that inline next to the Save button.
 *
 *   4. SMTP password field is <input type="password"> with a placeholder
 *      "•••• (currently set)" when smtp.password_set is true. The user
 *      sees whether a password is configured without ever seeing the
 *      ciphertext. Leaving the field blank on save preserves the
 *      existing password (matches the server's null-sentinel semantics
 *      for smtp.password).
 *
 *   5. Housekeeping tab has destructive actions (VACUUM, Purge). Purge
 *      gets a confirm() dialog because it deletes rows permanently;
 *      VACUUM doesn't because it only rebuilds the file. Both buttons
 *      show a "running…" state while the request is in flight because
 *      VACUUM on a large DB takes seconds.
 *
 *   6. Reports tab shows the schedule list with inline add/remove +
 *      per-schedule Run Now. Editing a schedule doesn't open a modal —
 *      it's a form with Save button that updates the schedule array in
 *      place. Schedule CRUD via the schedules array in settings.json
 *      (not a separate API) so the scheduler's 60s tick picks up the
 *      new config on the next wake.
 *
 *   7. Theme tab duplicates the sidebar's theme toggle with explanatory
 *      text. The control in the sidebar is the fast path; the one
 *      here is the "Settings" path for users who discover the sidebar
 *      control is missing (it isn't, but the feature tracks user
 *      mental models).
 */

import * as api from '../api.js';
import '../components/info-tip.js';

const TABS = [
    { id: 'collection',   label: 'Collection',   icon: '⏱' },
    { id: 'smtp',         label: 'SMTP',         icon: '✉' },
    { id: 'alerts',       label: 'Alerts',       icon: '⚠' },
    { id: 'power',        label: 'Power',        icon: '⚡' },
    { id: 'housekeeping', label: 'Housekeeping', icon: '🧹' },
    { id: 'logging',      label: 'Logging',      icon: '📝' },
    { id: 'reports',      label: 'Reports',      icon: '📄' },
    { id: 'theme',        label: 'Theme',        icon: '🎨' },
];

let state = {
    settings: null,           // the current settings snapshot from GET
    activeTab: 'collection',
    tabPanels: new Map(),     // id → element (for tab switching)
    saveStatus: new Map(),    // id → {status: 'ok'|'err'|null, message: string}
};

/* ─── Small DOM helpers ─────────────────────────────────────────────────── */

function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function field(id, labelText, inputNode, infoText) {
    // A labeled form row. `inputNode` is a pre-built <input>/<select>/
    // <textarea>; we wrap it in a <div class="field"> with label + info-tip.
    const row = el('div', 'field');
    row.style.display = 'flex';
    row.style.flexDirection = 'column';
    row.style.gap = 'var(--space-1)';
    row.style.marginBottom = 'var(--space-4)';

    const labelRow = el('div');
    labelRow.style.display = 'flex';
    labelRow.style.alignItems = 'center';
    labelRow.style.gap = 'var(--space-2)';

    const label = el('label', null, labelText);
    label.setAttribute('for', id);
    label.style.fontSize = 'var(--font-size-sm)';
    label.style.color = 'var(--text-secondary)';
    label.style.fontWeight = 'var(--font-weight-medium)';
    labelRow.append(label);

    if (infoText) {
        const tip = document.createElement('info-tip');
        tip.setAttribute('text', infoText);
        labelRow.append(tip);
    }

    inputNode.id = id;
    row.append(labelRow, inputNode);
    return row;
}

function numberInput(name, value, min, max, step = 1) {
    const i = document.createElement('input');
    i.type = 'number';
    i.name = name;
    i.value = String(value ?? '');
    if (min !== undefined) i.min = String(min);
    if (max !== undefined) i.max = String(max);
    if (step !== undefined) i.step = String(step);
    return i;
}

function textInput(name, value, placeholder = '') {
    const i = document.createElement('input');
    i.type = 'text';
    i.name = name;
    i.value = value ?? '';
    if (placeholder) i.placeholder = placeholder;
    return i;
}

function passwordInput(name, placeholder = '') {
    const i = document.createElement('input');
    i.type = 'password';
    i.name = name;
    i.value = '';
    i.placeholder = placeholder;
    i.autocomplete = 'new-password';
    return i;
}

function emailInput(name, value, placeholder = '') {
    const i = document.createElement('input');
    i.type = 'email';
    i.name = name;
    i.value = value ?? '';
    if (placeholder) i.placeholder = placeholder;
    return i;
}

function selectInput(name, value, options) {
    const s = document.createElement('select');
    s.name = name;
    options.forEach(({ value: v, label }) => {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = label;
        if (String(v) === String(value)) opt.selected = true;
        s.append(opt);
    });
    return s;
}

function checkboxInput(name, checked) {
    const i = document.createElement('input');
    i.type = 'checkbox';
    i.name = name;
    i.checked = !!checked;
    return i;
}

// Phase 6c round 1: validated numeric read from an <input type="number">.
// Returns the parsed finite number on success, or null on failure. On
// failure calls reportValidity() to show the browser's native constraint
// message inline next to the offending field, so the user gets immediate
// feedback without a server round-trip.
//
// Using valueAsNumber instead of Number(input.value) correctly distinguishes
// "empty field" (NaN) from "literal zero" — Number("") is 0, which would
// silently send interval_seconds=0 to the server and trigger a 400 from
// Pydantic's ge=2 constraint with a generic error. Catching the failure
// client-side at the button boundary produces a better error UX.
function numericValue(input) {
    if (!input.checkValidity()) {
        input.reportValidity();
        return null;
    }
    const v = input.valueAsNumber;
    if (!Number.isFinite(v)) {
        input.reportValidity();
        return null;
    }
    return v;
}

function checkboxRow(id, labelText, checked, infoText) {
    // Horizontal layout: [checkbox] [label] [info-tip]
    const row = el('div', 'field');
    row.style.display = 'flex';
    row.style.alignItems = 'center';
    row.style.gap = 'var(--space-2)';
    row.style.marginBottom = 'var(--space-3)';

    const cb = checkboxInput(id, checked);
    cb.id = id;

    const label = el('label', null, labelText);
    label.setAttribute('for', id);
    label.style.fontSize = 'var(--font-size-sm)';
    label.style.color = 'var(--text-secondary)';

    row.append(cb, label);
    if (infoText) {
        const tip = document.createElement('info-tip');
        tip.setAttribute('text', infoText);
        row.append(tip);
    }
    return row;
}

/* ─── Save button + result display ──────────────────────────────────────── */

function saveButton(tabId, onClick) {
    const wrap = el('div');
    wrap.style.display = 'flex';
    wrap.style.alignItems = 'center';
    wrap.style.gap = 'var(--space-3)';
    wrap.style.marginTop = 'var(--space-4)';

    const btn = el('button', 'primary', 'Save');
    btn.type = 'button';

    const status = el('div');
    status.id = `save-status-${tabId}`;
    status.style.fontSize = 'var(--font-size-sm)';

    btn.addEventListener('click', async () => {
        btn.disabled = true;
        const prev = btn.textContent;
        btn.textContent = 'Saving…';
        status.textContent = '';
        try {
            // Phase 6c round 1 fix: capture the server's merged/
            // validated response and update state.settings with it.
            // Without this, switching tabs and coming back re-renders
            // with pre-save cached values — the user sees their
            // saved change "disappear" until a full page reload.
            // `putSettings` already returns the validated dict
            // (with password redaction) so we just plumb it through.
            const response = await onClick();
            if (response && typeof response === 'object') {
                state.settings = response;
            }
            status.textContent = 'Saved.';
            status.style.color = 'var(--success, #34c759)';
            setTimeout(() => { status.textContent = ''; }, 2500);
        } catch (err) {
            let message = err.message || 'Save failed';
            if (err.detail && Array.isArray(err.detail)) {
                const first = err.detail[0];
                if (first && first.loc) {
                    message += ` (${first.loc.join('.')} — ${first.msg || ''})`;
                }
            }
            status.textContent = message;
            status.style.color = 'var(--danger, #ff3b30)';
        } finally {
            btn.disabled = false;
            btn.textContent = prev;
        }
    });

    wrap.append(btn, status);
    return wrap;
}

/* ─── Tab renderers ─────────────────────────────────────────────────────── */

function renderCollectionTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Collection'));
    const subtitle = el('div', 'subtitle',
        'How often the collector samples nvidia-smi and flushes to SQLite.');
    header.append(subtitle);
    panel.append(header);

    const c = state.settings.collection || {};

    const intervalInput = numberInput('interval_seconds', c.interval_seconds, 2, 300);
    panel.append(field(
        'collection-interval',
        'Poll interval (seconds)',
        intervalInput,
        'How often the collector queries nvidia-smi. Lower values produce more responsive charts but increase CPU usage. Changes apply within one interval of saving — no restart required.',
    ));

    const flushInput = numberInput('flush_interval_seconds', c.flush_interval_seconds, 5, 3600);
    panel.append(field(
        'collection-flush',
        'Flush interval (seconds)',
        flushInput,
        'How often buffered readings are committed to the database. This is the worst-case data-loss window if the container crashes uncleanly. A clean docker stop always flushes before exit.',
    ));

    panel.append(saveButton('collection', async () => {
        const interval = numericValue(intervalInput);
        const flush = numericValue(flushInput);
        if (interval === null || flush === null) return null;
        return api.putSettings({
            collection: {
                interval_seconds: interval,
                flush_interval_seconds: flush,
            },
        });
    }));

    return panel;
}

function renderSmtpTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'SMTP'));
    header.append(el('div', 'subtitle',
        'Outgoing mail server for scheduled reports and test emails.'));
    panel.append(header);

    const s = state.settings.smtp || {};

    const hostInput = textInput('host', s.host, 'smtp.example.com');
    panel.append(field('smtp-host', 'Host', hostInput,
        'Hostname or IP of your SMTP relay. Leave empty to disable email entirely.'));

    const portInput = numberInput('port', s.port || 587, 1, 65535);
    panel.append(field('smtp-port', 'Port', portInput,
        'Standard ports: 587 (STARTTLS), 465 (implicit TLS / SMTPS), 25 (plain).'));

    const userInput = textInput('user', s.user, 'user@example.com');
    panel.append(field('smtp-user', 'Username', userInput,
        'Auth username. Leave empty if your relay accepts anonymous mail (local MTAs, docker-compose Mailpit, etc).'));

    const passInput = passwordInput('password',
        s.password_set ? '•••• (currently set — leave empty to preserve)' : 'new password');
    panel.append(field('smtp-password', 'Password', passInput,
        'Leave empty to preserve the existing password. Enter a value to change it. Use the "Clear password" button below to fully remove the saved password.'));

    // Phase 6c round 1: the password help text used to tell users
    // to "enter a single space then delete to clear", but the save
    // logic maps empty → null (preserve), so that path silently
    // does nothing. A dedicated Clear button is unambiguous: it
    // sends smtp.password = "" which the server maps to the
    // four-way sentinel's "clear" branch and persists
    // password_enc = "". Only rendered when a password is
    // currently set — otherwise there's nothing to clear.
    if (s.password_set) {
        const clearWrap = document.createElement('div');
        clearWrap.style.display = 'flex';
        clearWrap.style.alignItems = 'center';
        clearWrap.style.gap = 'var(--space-2)';
        clearWrap.style.marginTop = 'calc(-1 * var(--space-3))';
        clearWrap.style.marginBottom = 'var(--space-4)';

        const clearBtn = el('button', 'small', 'Clear password');
        clearBtn.type = 'button';
        const clearStatus = el('span');
        clearStatus.style.fontSize = 'var(--font-size-sm)';
        clearStatus.style.color = 'var(--text-tertiary)';

        clearBtn.addEventListener('click', async () => {
            if (!confirm('Clear the saved SMTP password? You will need to re-enter it before the next test email or scheduled report can send.')) {
                return;
            }
            clearBtn.disabled = true;
            clearBtn.textContent = 'Clearing…';
            try {
                // Explicit empty string, NOT null — the server
                // maps "" to the "clear" branch of the password
                // sentinel. null would preserve the existing
                // ciphertext and the button would silently no-op.
                await api.putSettings({ smtp: { password: '' } });
                clearStatus.textContent = 'Password cleared. Enter a new one to re-enable auth.';
                clearStatus.style.color = 'var(--success)';
                passInput.placeholder = 'new password';
            } catch (err) {
                clearStatus.textContent = `Clear failed: ${err.message}`;
                clearStatus.style.color = 'var(--danger)';
            } finally {
                clearBtn.disabled = false;
                clearBtn.textContent = 'Clear password';
            }
        });

        clearWrap.append(clearBtn, clearStatus);
        panel.append(clearWrap);
    }

    const fromInput = emailInput('from', s.from, 'gpu-monitor@example.com');
    panel.append(field('smtp-from', 'From address', fromInput,
        'The "From:" header on sent messages. Most relays require this to match the authenticated user.'));

    const tlsSelect = selectInput('tls', s.tls || 'starttls', [
        { value: 'starttls', label: 'STARTTLS (port 587, recommended)' },
        { value: 'tls',      label: 'Implicit TLS (port 465, SMTPS)' },
        { value: 'none',     label: 'None (local relays only)' },
    ]);
    panel.append(field('smtp-tls', 'Encryption', tlsSelect,
        'TLS mode. Most modern relays want STARTTLS on 587. "None" should only be used for loopback relays — never against a real provider.'));

    // Save button + Test button row
    const actionRow = el('div');
    actionRow.style.display = 'flex';
    actionRow.style.alignItems = 'center';
    actionRow.style.gap = 'var(--space-3)';
    actionRow.style.marginTop = 'var(--space-4)';

    const saveBtn = el('button', 'primary', 'Save');
    saveBtn.type = 'button';

    const testBtn = el('button', null, 'Send test email');
    testBtn.type = 'button';

    const status = el('div');
    status.style.fontSize = 'var(--font-size-sm)';

    const setStatus = (text, kind) => {
        status.textContent = text;
        status.style.color = kind === 'err' ? 'var(--danger, #ff3b30)'
                           : kind === 'ok'  ? 'var(--success, #34c759)'
                           : 'var(--text-tertiary)';
    };

    saveBtn.addEventListener('click', async () => {
        const port = numericValue(portInput);
        if (port === null) return;
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving…';
        setStatus('', null);
        try {
            // Password sentinel: empty string = preserve (we send null),
            // non-empty = new password. The server does the right thing
            // based on this mapping (null = preserve, "" = clear, value
            // = set). We prefer preserve-on-empty because most edits
            // won't touch the password. A dedicated Clear password
            // button below the field handles the explicit clear path.
            const passwordValue = passInput.value === ''
                ? null
                : passInput.value;
            const response = await api.putSettings({
                smtp: {
                    host: hostInput.value,
                    port,
                    user: userInput.value,
                    password: passwordValue,
                    from: fromInput.value,
                    tls: tlsSelect.value,
                },
            });
            // Phase 6c round 1: persist the server-validated
            // response into state.settings so re-rendering this
            // tab shows the freshly-saved values instead of the
            // stale snapshot we loaded at mount.
            if (response && typeof response === 'object') {
                state.settings = response;
            }
            // Clear the password field after successful save so the
            // placeholder can switch to "currently set" on re-render
            passInput.value = '';
            setStatus('Saved.', 'ok');
        } catch (err) {
            setStatus(`Save failed: ${err.message}`, 'err');
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = 'Save';
        }
    });

    testBtn.addEventListener('click', async () => {
        testBtn.disabled = true;
        testBtn.textContent = 'Sending…';
        setStatus('', null);
        try {
            const result = await api.testSmtp();
            setStatus(`Test email sent to ${result.to}`, 'ok');
        } catch (err) {
            setStatus(`Test failed: ${err.message}`, 'err');
        } finally {
            testBtn.disabled = false;
            testBtn.textContent = 'Send test email';
        }
    });

    actionRow.append(saveBtn, testBtn, status);
    panel.append(actionRow);
    return panel;
}

function renderAlertsTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Alerts'));
    header.append(el('div', 'subtitle',
        'Thresholds for triggering visual + audible alerts on the dashboard.'));
    panel.append(header);

    const a = state.settings.alerts || {};

    const tempInput = numberInput('temperature_c', a.temperature_c, 0, 150);
    panel.append(field('alerts-temp', 'Temperature threshold (°C)', tempInput,
        'Alert fires when any GPU exceeds this temperature. Default 80 °C matches NVIDIA\'s recommended safe operating range.'));

    const utilInput = numberInput('utilization_pct', a.utilization_pct, 0, 100);
    panel.append(field('alerts-util', 'Utilization threshold (%)', utilInput,
        'Sustained utilization above this value triggers an alert. Default 100% is essentially "never alert on util"; drop to 90% for early warnings.'));

    const powerInput = numberInput('power_w', a.power_w, 0, 2000);
    panel.append(field('alerts-power', 'Power threshold (W)', powerInput,
        'Per-GPU power draw alert. Should be set just below your card\'s TDP so sustained high loads trigger a warning.'));

    const cooldownInput = numberInput('cooldown_seconds', a.cooldown_seconds, 2, 600);
    panel.append(field('alerts-cooldown', 'Cooldown (seconds)', cooldownInput,
        'Minimum time between alert firings for the same metric. Prevents a flapping value from spamming notifications.'));

    panel.append(checkboxRow('alerts-sound', 'Sound enabled', a.sound_enabled,
        'Play an audio cue when an alert fires. Requires user interaction on the page before the browser will autoplay audio.'));

    panel.append(checkboxRow('alerts-notifications', 'Desktop notifications', a.notifications_enabled,
        'Use the browser\'s Notification API for system-level alerts. Browser permission is requested the first time you enable this.'));

    panel.append(saveButton('alerts', async () => {
        const temp = numericValue(tempInput);
        const util = numericValue(utilInput);
        const power = numericValue(powerInput);
        const cooldown = numericValue(cooldownInput);
        if (temp === null || util === null || power === null || cooldown === null) {
            return null;
        }
        return api.putSettings({
            alerts: {
                temperature_c: temp,
                utilization_pct: util,
                power_w: power,
                cooldown_seconds: cooldown,
                sound_enabled: panel.querySelector('#alerts-sound').checked,
                notifications_enabled: panel.querySelector('#alerts-notifications').checked,
            },
        });
    }));

    return panel;
}

function renderPowerTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Power'));
    header.append(el('div', 'subtitle',
        'Electricity rate used for the Power view\'s cost calculation.'));
    panel.append(header);

    const p = state.settings.power || {};

    const rateInput = numberInput('rate_per_kwh', p.rate_per_kwh || 0, 0, 10, 0.0001);
    panel.append(field('power-rate', 'Rate per kWh', rateInput,
        'Your electricity tariff. Used to convert integrated Wh into a cost estimate on the Power view. Leave at 0 to hide cost displays.'));

    const currencyInput = textInput('currency', p.currency || '$', '$');
    currencyInput.maxLength = 4;
    panel.append(field('power-currency', 'Currency symbol', currencyInput,
        'Single-character (or short) currency symbol displayed next to cost values. Examples: $ € £ ¥.'));

    panel.append(saveButton('power', async () => {
        const rate = numericValue(rateInput);
        if (rate === null) return null;
        return api.putSettings({
            power: {
                rate_per_kwh: rate,
                currency: currencyInput.value,
            },
        });
    }));

    return panel;
}

function renderLoggingTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Logging'));
    header.append(el('div', 'subtitle',
        'Log rotation thresholds. Rotation fires when EITHER threshold is exceeded.'));
    panel.append(header);

    const l = state.settings.logging || {};

    const sizeInput = numberInput('max_size_mb', l.max_size_mb, 1, 100);
    panel.append(field('log-size', 'Max size per log (MB)', sizeInput,
        'Rotated when any single log file exceeds this size.'));

    const ageInput = numberInput('max_age_hours', l.max_age_hours, 1, 720);
    panel.append(field('log-age', 'Max age (hours)', ageInput,
        'Rotated when the log file is older than this. Default 25 h keeps the last day of logs plus a rollover margin.'));

    panel.append(saveButton('logging', async () => {
        const size = numericValue(sizeInput);
        const age = numericValue(ageInput);
        if (size === null || age === null) return null;
        return api.putSettings({
            logging: {
                max_size_mb: size,
                max_age_hours: age,
            },
        });
    }));

    return panel;
}

function renderThemeTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Theme'));
    header.append(el('div', 'subtitle',
        'Default theme mode when the dashboard loads.'));
    panel.append(header);

    const t = state.settings.theme || {};

    const modeSelect = selectInput('default_mode', t.default_mode || 'auto', [
        { value: 'auto',  label: 'Auto (follow OS preference)' },
        { value: 'light', label: 'Light' },
        { value: 'dark',  label: 'Dark' },
    ]);
    panel.append(field('theme-default', 'Default mode', modeSelect,
        'The sidebar has a live toggle that overrides this per-session. This setting is the default on a fresh page load.'));

    panel.append(saveButton('theme', async () => {
        return api.putSettings({
            theme: { default_mode: modeSelect.value },
        });
    }));

    return panel;
}

function renderHousekeepingTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Housekeeping'));
    header.append(el('div', 'subtitle',
        'Database size + retention + manual maintenance actions.'));
    panel.append(header);

    const h = state.settings.housekeeping || {};

    // Retention (part of the settings form)
    const retentionInput = numberInput('retention_days', h.retention_days, 1, 365);
    panel.append(field('housekeeping-retention', 'Retention (days)', retentionInput,
        'The nightly clean_old_data sweep deletes rows older than this. Changes apply at the next midnight sweep.'));

    panel.append(saveButton('housekeeping', async () => {
        const retention = numericValue(retentionInput);
        if (retention === null) return null;
        return api.putSettings({
            housekeeping: { retention_days: retention },
        });
    }));

    // DB info panel (read-only)
    const infoBox = el('div');
    infoBox.style.marginTop = 'var(--space-5)';
    infoBox.style.padding = 'var(--space-4)';
    infoBox.style.background = 'var(--bg-tertiary)';
    infoBox.style.borderRadius = 'var(--radius-md)';
    infoBox.style.fontFamily = 'var(--font-mono)';
    infoBox.style.fontSize = 'var(--font-size-sm)';

    const infoTitle = el('div', null, 'Database status');
    infoTitle.style.fontWeight = 'var(--font-weight-semibold)';
    infoTitle.style.marginBottom = 'var(--space-2)';
    infoTitle.style.fontFamily = 'var(--font-system)';
    infoBox.append(infoTitle);

    const infoContent = el('div', null, 'Loading…');
    infoBox.append(infoContent);
    panel.append(infoBox);

    // Populate the DB info asynchronously
    api.getDbInfo().then(info => {
        const mb = (info.size_bytes / (1024 * 1024)).toFixed(2);
        const perGpu = (info.row_count_per_gpu || [])
            .map(r => `GPU ${r.gpu_index}: ${r.row_count.toLocaleString()} rows`)
            .join(' · ') || '—';
        infoContent.textContent =
            `${mb} MB · ${info.row_count.toLocaleString()} total rows · ${perGpu}`;
    }).catch(() => {
        infoContent.textContent = '(unable to read db-info)';
    });

    // Action buttons
    const actionRow = el('div');
    actionRow.style.display = 'flex';
    actionRow.style.gap = 'var(--space-3)';
    actionRow.style.marginTop = 'var(--space-4)';
    actionRow.style.alignItems = 'center';
    actionRow.style.flexWrap = 'wrap';

    const vacuumBtn = el('button', null, 'Run VACUUM now');
    vacuumBtn.type = 'button';

    const purgeInput = numberInput('purge_days', 7, 1, 365);
    purgeInput.style.width = '80px';
    const purgeBtn = el('button', null, 'Purge older than');
    purgeBtn.type = 'button';
    const purgeDaysLabel = el('span', null, ' days');

    const actionStatus = el('div');
    actionStatus.style.fontSize = 'var(--font-size-sm)';
    actionStatus.style.marginLeft = 'var(--space-2)';

    vacuumBtn.addEventListener('click', async () => {
        vacuumBtn.disabled = true;
        vacuumBtn.textContent = 'VACUUMing…';
        actionStatus.textContent = '';
        try {
            const result = await api.vacuumDb();
            const freedMb = (result.freed_bytes / (1024 * 1024)).toFixed(2);
            actionStatus.textContent = `VACUUM freed ${freedMb} MB`;
            actionStatus.style.color = 'var(--success)';
        } catch (err) {
            actionStatus.textContent = `VACUUM failed: ${err.message}`;
            actionStatus.style.color = 'var(--danger)';
        } finally {
            vacuumBtn.disabled = false;
            vacuumBtn.textContent = 'Run VACUUM now';
        }
    });

    purgeBtn.addEventListener('click', async () => {
        const days = Number(purgeInput.value);
        if (!confirm(`Delete all rows older than ${days} days? This cannot be undone.`)) {
            return;
        }
        purgeBtn.disabled = true;
        purgeBtn.textContent = 'Purging…';
        actionStatus.textContent = '';
        try {
            const result = await api.purgeOldData(days);
            actionStatus.textContent = `Purged ${result.rows_deleted.toLocaleString()} rows`;
            actionStatus.style.color = 'var(--success)';
        } catch (err) {
            actionStatus.textContent = `Purge failed: ${err.message}`;
            actionStatus.style.color = 'var(--danger)';
        } finally {
            purgeBtn.disabled = false;
            purgeBtn.textContent = 'Purge older than';
        }
    });

    actionRow.append(vacuumBtn, purgeBtn, purgeInput, purgeDaysLabel, actionStatus);
    panel.append(actionRow);

    return panel;
}

function renderReportsTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Reports'));
    header.append(el('div', 'subtitle',
        'Scheduled email reports. Cron expressions are evaluated in the container\'s TZ.'));
    panel.append(header);

    const schedules = Array.isArray(state.settings.schedules)
        ? state.settings.schedules
        : [];

    const list = el('div');
    list.style.display = 'flex';
    list.style.flexDirection = 'column';
    list.style.gap = 'var(--space-3)';

    if (schedules.length === 0) {
        const empty = el('div', null, 'No scheduled reports configured.');
        empty.style.color = 'var(--text-tertiary)';
        empty.style.fontSize = 'var(--font-size-sm)';
        empty.style.padding = 'var(--space-3) 0';
        list.append(empty);
    } else {
        schedules.forEach(schedule => list.append(renderScheduleCard(schedule)));
    }

    panel.append(list);

    // Add-new form
    const addWrap = el('div');
    addWrap.style.marginTop = 'var(--space-5)';
    addWrap.style.padding = 'var(--space-4)';
    addWrap.style.background = 'var(--bg-tertiary)';
    addWrap.style.borderRadius = 'var(--radius-md)';

    const addTitle = el('div', null, 'Add a new schedule');
    addTitle.style.fontWeight = 'var(--font-weight-semibold)';
    addTitle.style.marginBottom = 'var(--space-3)';
    addWrap.append(addTitle);

    const idInput = textInput('new_id', '', 'daily-0800');
    addWrap.append(field('report-new-id', 'ID', idInput,
        'Unique identifier used by run-now and the scheduler. Cannot be changed later without removing and re-adding.'));

    const templateSelect = selectInput('new_template', 'daily', [
        { value: 'daily',   label: 'Daily (last 24 hours)' },
        { value: 'weekly',  label: 'Weekly (last 7 days)' },
        { value: 'monthly', label: 'Monthly (last 30 days)' },
    ]);
    addWrap.append(field('report-new-template', 'Template', templateSelect));

    const cronInput = textInput('new_cron', '0 8 * * *', '0 8 * * *');
    addWrap.append(field('report-new-cron', 'Cron expression', cronInput,
        'Standard 5-field cron. "0 8 * * *" = every day at 08:00. Evaluated in the container\'s TZ environment variable.'));

    const recipientsInput = textInput('new_recipients', '', 'a@example.com, b@example.com');
    addWrap.append(field('report-new-recipients', 'Recipients (comma-separated)', recipientsInput));

    const addBtn = el('button', 'primary', 'Add schedule');
    addBtn.type = 'button';
    const addStatus = el('div');
    addStatus.style.fontSize = 'var(--font-size-sm)';
    addStatus.style.marginTop = 'var(--space-2)';

    addBtn.addEventListener('click', async () => {
        const recipients = recipientsInput.value
            .split(',')
            .map(s => s.trim())
            .filter(Boolean);
        const newId = idInput.value.trim();
        if (!newId || recipients.length === 0) {
            addStatus.textContent = 'ID and at least one recipient are required.';
            addStatus.style.color = 'var(--danger)';
            return;
        }
        // Phase 6c round 1: reject duplicate schedule IDs at the
        // client boundary. The scheduler's fire-id dict and the
        // server's run-now handler both key on schedule.id; adding
        // two entries with the same id would leave the second one
        // effectively dead (run-now finds the first via next(),
        // the scheduler stamps only the first's last_run_epoch on
        // each tick). Better to refuse at submit time with a clear
        // message than produce silent unreachable state.
        if (schedules.some(s => s && s.id === newId)) {
            addStatus.textContent = `A schedule with id "${newId}" already exists. Remove it first or pick a different id.`;
            addStatus.style.color = 'var(--danger)';
            return;
        }
        const nextSchedules = [
            ...schedules,
            {
                id: newId,
                template: templateSelect.value,
                cron: cronInput.value,
                recipients,
                enabled: true,
                last_run_epoch: null,
            },
        ];
        addBtn.disabled = true;
        addBtn.textContent = 'Adding…';
        addStatus.textContent = '';
        try {
            await api.putSettings({ schedules: nextSchedules });
            // Re-fetch and re-render this tab
            await reloadAndReRender();
            addStatus.textContent = '';
        } catch (err) {
            addStatus.textContent = `Failed: ${err.message}`;
            addStatus.style.color = 'var(--danger)';
        } finally {
            addBtn.disabled = false;
            addBtn.textContent = 'Add schedule';
        }
    });

    addWrap.append(addBtn, addStatus);
    panel.append(addWrap);

    return panel;
}

function renderScheduleCard(schedule) {
    const card = el('div');
    card.style.padding = 'var(--space-3) var(--space-4)';
    card.style.border = '1px solid var(--border-subtle)';
    card.style.borderRadius = 'var(--radius-md)';
    card.style.display = 'flex';
    card.style.flexDirection = 'column';
    card.style.gap = 'var(--space-2)';

    const topRow = el('div');
    topRow.style.display = 'flex';
    topRow.style.justifyContent = 'space-between';
    topRow.style.alignItems = 'baseline';

    const name = el('div');
    name.style.fontWeight = 'var(--font-weight-semibold)';
    name.textContent = schedule.id;
    const meta = el('div');
    meta.style.fontSize = 'var(--font-size-sm)';
    meta.style.color = 'var(--text-tertiary)';
    meta.style.fontFamily = 'var(--font-mono)';
    meta.textContent = `${schedule.template} · ${schedule.cron}`;
    topRow.append(name, meta);
    card.append(topRow);

    const recipients = el('div');
    recipients.style.fontSize = 'var(--font-size-sm)';
    recipients.style.color = 'var(--text-secondary)';
    recipients.textContent = 'To: ' + (schedule.recipients || []).join(', ');
    card.append(recipients);

    if (schedule.last_run_epoch) {
        const lastRun = el('div');
        lastRun.style.fontSize = 'var(--font-size-sm)';
        lastRun.style.color = 'var(--text-tertiary)';
        const d = new Date(schedule.last_run_epoch * 1000);
        lastRun.textContent = 'Last run: ' + d.toLocaleString();
        card.append(lastRun);
    }

    // Action buttons
    const actionRow = el('div');
    actionRow.style.display = 'flex';
    actionRow.style.gap = 'var(--space-2)';
    actionRow.style.marginTop = 'var(--space-2)';

    const runBtn = el('button', 'small', 'Run now');
    runBtn.type = 'button';
    const removeBtn = el('button', 'small', 'Remove');
    removeBtn.type = 'button';

    const runStatus = el('span');
    runStatus.style.fontSize = 'var(--font-size-sm)';
    runStatus.style.marginLeft = 'var(--space-2)';

    runBtn.addEventListener('click', async () => {
        runBtn.disabled = true;
        runBtn.textContent = 'Sending…';
        runStatus.textContent = '';
        try {
            const result = await api.runScheduleNow(schedule.id);
            runStatus.textContent = `Sent at ${new Date(result.last_run_epoch * 1000).toLocaleTimeString()}`;
            runStatus.style.color = 'var(--success)';
        } catch (err) {
            runStatus.textContent = `Failed: ${err.message}`;
            runStatus.style.color = 'var(--danger)';
        } finally {
            runBtn.disabled = false;
            runBtn.textContent = 'Run now';
        }
    });

    removeBtn.addEventListener('click', async () => {
        if (!confirm(`Remove schedule "${schedule.id}"?`)) return;
        const current = Array.isArray(state.settings.schedules)
            ? state.settings.schedules
            : [];
        const next = current.filter(s => s.id !== schedule.id);
        try {
            await api.putSettings({ schedules: next });
            await reloadAndReRender();
        } catch (err) {
            runStatus.textContent = `Remove failed: ${err.message}`;
            runStatus.style.color = 'var(--danger)';
        }
    });

    actionRow.append(runBtn, removeBtn, runStatus);
    card.append(actionRow);

    return card;
}

/* ─── Tab switching + mount ─────────────────────────────────────────────── */

const TAB_RENDERERS = {
    collection: renderCollectionTab,
    smtp:       renderSmtpTab,
    alerts:     renderAlertsTab,
    power:      renderPowerTab,
    housekeeping: renderHousekeepingTab,
    logging:    renderLoggingTab,
    reports:    renderReportsTab,
    theme:      renderThemeTab,
};

function buildTabStrip(onSelect) {
    const tabs = el('div', 'tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.style.flexWrap = 'wrap';
    tabs.style.marginBottom = 'var(--space-4)';

    TABS.forEach(tab => {
        const btn = el('button', null, `${tab.icon} ${tab.label}`);
        btn.type = 'button';
        btn.setAttribute('data-tab-id', tab.id);
        btn.setAttribute('role', 'tab');
        if (tab.id === state.activeTab) {
            btn.setAttribute('aria-current', 'true');
        }
        btn.addEventListener('click', () => {
            state.activeTab = tab.id;
            tabs.querySelectorAll('button').forEach(b => {
                if (b.getAttribute('data-tab-id') === tab.id) {
                    b.setAttribute('aria-current', 'true');
                } else {
                    b.removeAttribute('aria-current');
                }
            });
            onSelect(tab.id);
        });
        tabs.append(btn);
    });

    return tabs;
}

let _container = null;

async function reloadAndReRender() {
    // Fetch fresh settings and re-render the active tab only. Used by
    // schedules add/remove which mutate the schedules array and need
    // the panel to pick up the new state.
    state.settings = await api.getSettings();
    const existing = document.getElementById('settings-panel-mount');
    if (!existing) return;
    const renderer = TAB_RENDERERS[state.activeTab] || renderCollectionTab;
    existing.replaceChildren(renderer());
}

export const settingsView = {
    name: 'settings',

    async mount(container) {
        _container = container;

        // Header
        const header = document.createElement('header');
        const h1 = document.createElement('h1');
        h1.textContent = 'Settings';
        const subtitle = document.createElement('div');
        subtitle.className = 'subtitle';
        subtitle.textContent = 'Collection, SMTP, alerts, housekeeping, and report schedules';
        header.append(h1, subtitle);
        container.append(header);

        // Fetch initial state
        try {
            state.settings = await api.getSettings();
        } catch {
            state.settings = {};
        }

        // Tab strip
        const panelMount = document.createElement('div');
        panelMount.id = 'settings-panel-mount';

        const tabs = buildTabStrip(tabId => {
            const renderer = TAB_RENDERERS[tabId] || renderCollectionTab;
            panelMount.replaceChildren(renderer());
        });
        container.append(tabs);
        container.append(panelMount);

        // Initial render of the active tab
        const renderer = TAB_RENDERERS[state.activeTab] || renderCollectionTab;
        panelMount.replaceChildren(renderer());
    },

    unmount() {
        _container = null;
        state.tabPanels.clear();
        state.saveStatus.clear();
    },
};
