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
import * as alerts from '../alerts.js';
import '../components/info-tip.js';
import { showToast } from '../widgets/toast.js';

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
// Pydantic's ge=1 constraint with a generic error. Catching the failure
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

/* ─── Autosave + toast infrastructure ───────────────────────────────────── */
//
// Phase 7 polish round: the Settings view was rewritten from an explicit
// "change fields, click Save per tab" flow to a macOS-style autosave-on-
// change flow with floating toast feedback. The old saveButton() helper
// has been removed. Each input binds itself to autosave() via one of the
// bindXxx helpers below, the helper builds a partial-merge patch that
// targets only the specific field that changed, and the server deep-
// merges it into settings.json — meaning multiple concurrent edits on
// different fields compose without stepping on each other.
//
// Design notes:
//
//   1. Number inputs save on the native `change` event, which the
//      browser fires on blur or Enter — not on every keystroke. This
//      means typing "42" in a retention field triggers one save, not
//      two. For text inputs we use `blur` explicitly because `change`
//      on text inputs doesn't fire on every commit point (only when
//      the value actually differs from the initial), which occasionally
//      misses save opportunities. Blur is slightly more chatty but
//      deterministic.
//
//   2. Checkboxes and selects save immediately on `change` — the
//      event fires the moment the user clicks/changes, which is the
//      desired UX (toggle immediately applies, no save-then-wait).
//
//   3. Numeric validation still goes through numericValue() — if
//      the input fails its HTML5 constraints, numericValue calls
//      reportValidity() and returns null, which we use as "don't
//      send a save request". The user sees the native browser
//      constraint message inline; no toast fires.
//
//   4. The SMTP password field is special: it saves on blur, but
//      ONLY when the value is non-empty. An empty value means
//      "preserve existing password" (the server's null-sentinel
//      behavior), and a blur with empty field is a no-op rather
//      than a save — which matches the user's mental model
//      ("I didn't change the password, so don't touch it").
//
//   5. Server validation errors are surfaced via the toast with
//      the Pydantic-formatted detail message. The offending field
//      is NOT rolled back — the user sees their bad value still
//      in the input, can correct it, and the next change event
//      fires another autosave attempt.
//
//   6. The autosave helper also updates state.settings with the
//      server's validated response. This keeps switching tabs
//      consistent with what was just saved, exactly the same way
//      the old saveButton flow did.

function debounced(fn, ms = 500) {
    let t = null;
    return (...args) => {
        if (t) clearTimeout(t);
        t = setTimeout(() => fn(...args), ms);
    };
}

async function autosave(patch) {
    // Central autosave dispatcher. Every bindXxx helper calls this
    // with a shaped patch like `{ collection: { interval_seconds: 4 } }`
    // and it takes care of the PUT + state update + toast feedback.
    try {
        const response = await api.putSettings(patch);
        if (response && typeof response === 'object') {
            state.settings = response;
        }
        showToast('Saved', 'success');
        return response;
    } catch (err) {
        let message = err.message || 'Save failed';
        if (err.detail && Array.isArray(err.detail)) {
            const first = err.detail[0];
            if (first && first.loc) {
                message += ` (${first.loc.join('.')} — ${first.msg || ''})`;
            }
        }
        showToast(message, 'error');
        throw err;
    }
}

// Bind a number input: saves on `change` (fires on blur/Enter, not
// every keystroke). `buildPatch(value)` should return the full patch
// object shape, e.g. `v => ({ collection: { interval_seconds: v } })`.
function bindNumberChange(input, buildPatch) {
    input.addEventListener('change', () => {
        const v = numericValue(input);
        if (v === null) return;  // Invalid — native validity UI already
                                 // shown by numericValue().
        autosave(buildPatch(v)).catch(() => {});
    });
}

// Bind a text input: saves on `blur`. Chatty compared to `change`
// but deterministic — any time the user tabs/clicks away from the
// field, the current value is committed.
function bindTextBlur(input, buildPatch) {
    // Track the last committed value so we don't fire a save when
    // the user just clicks in and clicks out without editing.
    let last = input.value;
    input.addEventListener('blur', () => {
        if (input.value === last) return;
        last = input.value;
        autosave(buildPatch(input.value)).catch(() => {});
    });
}

// Bind a <select>: fires immediately on `change` (the moment the
// user picks a new option).
function bindSelectChange(select, buildPatch) {
    select.addEventListener('change', () => {
        autosave(buildPatch(select.value)).catch(() => {});
    });
}

// Bind a checkbox: fires immediately on `change`. Uses the
// checkbox's current .checked value rather than a parameter
// because checkboxes have no interesting "pre-change" vs
// "post-change" distinction — the event fires after the toggle.
function bindCheckboxChange(input, buildPatch) {
    input.addEventListener('change', () => {
        autosave(buildPatch(input.checked)).catch(() => {});
    });
}

/* ─── Tab renderers ─────────────────────────────────────────────────────── */

function renderCollectionTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Collection'));
    const subtitle = el('div', 'subtitle',
        'How often the collector samples nvidia-smi and flushes to SQLite. Changes save automatically.');
    header.append(subtitle);
    panel.append(header);

    const c = state.settings.collection || {};

    // v2.1.0: minimum lowered from 2 → 1. NVML's internal utilization
    // sampling window is 1 s, so 1 Hz is the meaningful floor.
    const intervalInput = numberInput('interval_seconds', c.interval_seconds, 1, 300);
    bindNumberChange(intervalInput, v => ({
        collection: { interval_seconds: v },
    }));
    panel.append(field(
        'collection-interval',
        'Poll interval (seconds)',
        intervalInput,
        'How often the collector samples NVML for GPU metrics. Range 1–300 s; 1 s is the meaningful floor (NVML averages utilization over a ~1 s window internally). Lower values produce more granular charts but more DB rows; per-tick CPU is ~1–2 ms via pynvml. Changes apply within one interval of saving — no restart required.',
    ));

    const flushInput = numberInput('flush_interval_seconds', c.flush_interval_seconds, 5, 3600);
    bindNumberChange(flushInput, v => ({
        collection: { flush_interval_seconds: v },
    }));
    panel.append(field(
        'collection-flush',
        'Flush interval (seconds)',
        flushInput,
        'How often buffered readings are committed to the database. This is the worst-case data-loss window if the container crashes uncleanly. A clean docker stop always flushes before exit.',
    ));

    return panel;
}

function renderSmtpTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'SMTP'));
    header.append(el('div', 'subtitle',
        'Outgoing mail server for scheduled reports and test emails. Changes save automatically.'));
    panel.append(header);

    const s = state.settings.smtp || {};

    // Wrap all the credential fields in a <form> element. Browsers
    // expect <input type="password"> to live inside a <form> ancestor
    // for password-manager autofill/save-to-vault integration —
    // Chrome specifically logs "Password field is not contained in a
    // form" when the field stands alone in a <div>. The form has no
    // submit action and its onsubmit preventDefault() swallows any
    // accidental Enter-key submissions, so the autosave JS handlers
    // remain the single source of truth for field persistence. This
    // is a pure wrapper — no CSS or layout impact — so appending
    // siblings like the Clear password block and the action row
    // either inside or outside the form works the same visually.
    const smtpForm = document.createElement('form');
    smtpForm.setAttribute('autocomplete', 'on');
    smtpForm.setAttribute('novalidate', '');  // we validate on change, not submit
    smtpForm.addEventListener('submit', (e) => e.preventDefault());
    panel.append(smtpForm);

    const hostInput = textInput('host', s.host, 'smtp.example.com');
    hostInput.setAttribute('autocomplete', 'off');  // SMTP host is not a login username
    bindTextBlur(hostInput, v => ({ smtp: { host: v } }));
    smtpForm.append(field('smtp-host', 'Host', hostInput,
        'Hostname or IP of your SMTP relay. Leave empty to disable email entirely.'));

    const portInput = numberInput('port', s.port || 587, 1, 65535);
    bindNumberChange(portInput, v => ({ smtp: { port: v } }));
    smtpForm.append(field('smtp-port', 'Port', portInput,
        'Standard ports: 587 (STARTTLS), 465 (implicit TLS / SMTPS), 25 (plain).'));

    const userInput = textInput('user', s.user, 'user@example.com');
    userInput.setAttribute('autocomplete', 'username');
    bindTextBlur(userInput, v => ({ smtp: { user: v } }));
    smtpForm.append(field('smtp-user', 'Username', userInput,
        'Auth username. Leave empty if your relay accepts anonymous mail (local MTAs, docker-compose Mailpit, etc).'));

    const passInput = passwordInput('password',
        s.password_set ? '•••• (currently set — leave empty to preserve)' : 'new password');
    // Hint browsers that this is a "current-password" field paired
    // with the username above, so password managers offer to
    // save/autofill the pair. The passwordInput helper defaults to
    // autocomplete="new-password" (which suppresses autofill); we
    // override that here so the Settings screen gets the password-
    // manager treatment the user expects from a login-ish form.
    passInput.setAttribute('autocomplete', 'current-password');
    // Password field is the only autosave edge case. It saves on blur,
    // but ONLY when the field is non-empty. An empty blur means "user
    // tabbed past without changing" and we preserve the existing
    // ciphertext (server maps null → preserve in the smtp.password
    // four-way sentinel). A non-empty blur sends the new password.
    // After a successful save we clear the field and re-render-prompt
    // the placeholder to reflect that a password is now set.
    passInput.addEventListener('blur', async () => {
        if (passInput.value === '') return;  // preserve existing
        try {
            await autosave({ smtp: { password: passInput.value } });
            // Clear the field so future visits show the masked placeholder
            // instead of the cleartext the user just typed.
            passInput.value = '';
            passInput.placeholder = '•••• (currently set — leave empty to preserve)';
        } catch {
            // Toast already fired by autosave(); leave the field alone
            // so the user can re-try or correct.
        }
    });
    smtpForm.append(field('smtp-password', 'Password', passInput,
        'Leave empty to preserve the existing password. Enter a value and tab away to change it. Use the "Clear password" button below to fully remove the saved password.'));

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
        // Clear button lives inside the form so password managers see
        // it as part of the "credentials cluster" — harmless but keeps
        // DOM locality with the password field it acts on.
        smtpForm.append(clearWrap);
    }

    const fromInput = emailInput('from', s.from, 'gpu-monitor@example.com');
    fromInput.setAttribute('autocomplete', 'off');  // not a login identifier
    bindTextBlur(fromInput, v => ({ smtp: { from: v } }));
    smtpForm.append(field('smtp-from', 'From address', fromInput,
        'The "From:" header on sent messages. Most relays require this to match the authenticated user.'));

    const tlsSelect = selectInput('tls', s.tls || 'starttls', [
        { value: 'starttls', label: 'STARTTLS (port 587, recommended)' },
        { value: 'tls',      label: 'Implicit TLS (port 465, SMTPS)' },
        { value: 'none',     label: 'None (local relays only)' },
    ]);
    bindSelectChange(tlsSelect, v => ({ smtp: { tls: v } }));
    smtpForm.append(field('smtp-tls', 'Encryption', tlsSelect,
        'TLS mode. Most modern relays want STARTTLS on 587. "None" should only be used for loopback relays — never against a real provider.'));

    // Test email section: a dedicated "Test recipient" input so the
    // user can send a test email to a specific address without having
    // to configure a full schedule in the Reports tab first. The
    // server already supports a {"to": "..."} override in the POST
    // body (Phase 6b design) — the UI just never exposed it.
    //
    // The recipient value is persisted to localStorage (like the
    // purge-days pattern) so it survives page reloads without
    // needing a settings.json key. If left empty, the server falls
    // back to smtp.user → smtp.from ("email yourself"), which is
    // shown in the success message so the user knows where it went.
    const TEST_RECIPIENT_KEY = 'gpu-monitor:smtp-test-recipient';
    let savedTestRecipient = '';
    try {
        savedTestRecipient = localStorage.getItem(TEST_RECIPIENT_KEY) || '';
    } catch { /* localStorage unavailable */ }

    const testRecipientInput = emailInput('test_recipient', savedTestRecipient, 'test@example.com');
    testRecipientInput.setAttribute('autocomplete', 'off');
    // Persist on blur so the value is remembered across sessions.
    testRecipientInput.addEventListener('blur', () => {
        try {
            localStorage.setItem(TEST_RECIPIENT_KEY, testRecipientInput.value);
        } catch { /* silently drop */ }
    });
    smtpForm.append(field('smtp-test-recipient', 'Test recipient', testRecipientInput,
        'Email address for the "Send test email" button below. Leave empty to send to the configured username/from address (i.e. email yourself). This value is remembered locally and is independent of report schedule recipients.'));

    // Action row: test email button + inline status. No Save button
    // — all SMTP fields above autosave on change/blur.
    const actionRow = el('div');
    actionRow.style.display = 'flex';
    actionRow.style.alignItems = 'center';
    actionRow.style.gap = 'var(--space-3)';
    actionRow.style.marginTop = 'var(--space-4)';

    const testBtn = el('button', 'primary', 'Send test email');
    testBtn.type = 'button';

    const status = el('div');
    status.style.fontSize = 'var(--font-size-sm)';

    testBtn.addEventListener('click', async () => {
        testBtn.disabled = true;
        testBtn.textContent = 'Sending…';
        status.textContent = '';
        try {
            // Pass the test recipient if the user entered one;
            // null/empty lets the server fall back to smtp.user/from.
            const to = testRecipientInput.value.trim() || null;
            const result = await api.testSmtp(to);
            status.textContent = `Test email sent to ${result.to}`;
            status.style.color = 'var(--success, #34c759)';
        } catch (err) {
            status.textContent = `Test failed: ${err.message}`;
            status.style.color = 'var(--danger, #ff3b30)';
        } finally {
            testBtn.disabled = false;
            testBtn.textContent = 'Send test email';
        }
    });

    actionRow.append(testBtn, status);
    panel.append(actionRow);
    return panel;
}

function renderAlertsTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Alerts'));
    header.append(el('div', 'subtitle',
        'Thresholds for triggering visual + audible alerts on the dashboard. Changes save automatically.'));
    panel.append(header);

    const a = state.settings.alerts || {};

    const tempInput = numberInput('temperature_c', a.temperature_c, 0, 150);
    bindNumberChange(tempInput, v => ({ alerts: { temperature_c: v } }));
    panel.append(field('alerts-temp', 'Temperature threshold (°C)', tempInput,
        'Alert fires when any GPU exceeds this temperature. Default 80 °C matches NVIDIA\'s recommended safe operating range.'));

    const utilInput = numberInput('utilization_pct', a.utilization_pct, 0, 100);
    bindNumberChange(utilInput, v => ({ alerts: { utilization_pct: v } }));
    panel.append(field('alerts-util', 'Utilization threshold (%)', utilInput,
        'Sustained utilization above this value triggers an alert. Default 100% is essentially "never alert on util"; drop to 90% for early warnings.'));

    const powerInput = numberInput('power_w', a.power_w, 0, 2000);
    bindNumberChange(powerInput, v => ({ alerts: { power_w: v } }));
    panel.append(field('alerts-power', 'Power threshold (W)', powerInput,
        'Per-GPU power draw alert. Should be set just below your card\'s TDP so sustained high loads trigger a warning.'));

    const cooldownInput = numberInput('cooldown_seconds', a.cooldown_seconds, 2, 600);
    bindNumberChange(cooldownInput, v => ({ alerts: { cooldown_seconds: v } }));
    panel.append(field('alerts-cooldown', 'Cooldown (seconds)', cooldownInput,
        'Minimum time between alert firings for the same metric. Prevents a flapping value from spamming notifications.'));

    panel.append(checkboxRow('alerts-sound', 'Sound enabled', a.sound_enabled,
        'Play an audio cue when an alert fires. Requires user interaction on the page before the browser will autoplay audio.'));
    const soundCheckbox = panel.querySelector('#alerts-sound');
    bindCheckboxChange(soundCheckbox, v => ({ alerts: { sound_enabled: v } }));

    panel.append(checkboxRow('alerts-notifications', 'Desktop notifications', a.notifications_enabled,
        'Use the browser\'s Notification API for system-level alerts. Browser permission is requested the first time you enable this.'));

    // Inline status element attached right below the checkbox so
    // the feedback appears where the user was looking.
    // Non-blocking — we never use alert() here because it's
    // modal and jars the user out of the Settings flow.
    const notificationsStatus = el('div');
    notificationsStatus.style.fontSize = 'var(--font-size-sm)';
    notificationsStatus.style.marginLeft = 'calc(var(--space-5) + var(--space-2))';
    notificationsStatus.style.marginTop = 'calc(-1 * var(--space-2))';
    notificationsStatus.style.marginBottom = 'var(--space-3)';
    panel.append(notificationsStatus);

    // Phase 7: request browser Notification permission ONLY when
    // the user explicitly flips the checkbox ON — never on page
    // load. This fixes the "spooky unprompted permission request"
    // UX smell that the Phase 4 hardcoded-to-configurable audit
    // specifically called out. We listen on the change event
    // rather than waiting for Save so the permission prompt is
    // tightly coupled to the user's intent expression.
    //
    // The autosave rewrite tweaks this flow: we still do the
    // permission dance on enable, but we ALSO autosave the setting
    // immediately when permission is granted OR when the user
    // disables the checkbox (the disable path never needs a
    // permission request). A non-granted result rolls the checkbox
    // back and NO save fires — the server's stored value stays
    // false.
    const notificationsCheckbox = panel.querySelector('#alerts-notifications');
    if (notificationsCheckbox) {
        notificationsCheckbox.addEventListener('change', async () => {
            notificationsStatus.textContent = '';

            // Disable path: no permission needed, just save false.
            if (!notificationsCheckbox.checked) {
                autosave({ alerts: { notifications_enabled: false } })
                    .catch(() => {});
                return;
            }

            // Enable path: request permission first, save only on grant.
            const result = await alerts.requestNotificationPermission();

            if (result === 'granted') {
                notificationsStatus.textContent = 'Notifications enabled.';
                notificationsStatus.style.color = 'var(--success)';
                autosave({ alerts: { notifications_enabled: true } })
                    .catch(() => {});
                return;
            }

            // Any non-granted result → uncheck and explain. No save.
            notificationsCheckbox.checked = false;
            notificationsStatus.style.color = 'var(--danger)';
            switch (result) {
                case 'denied':
                    notificationsStatus.textContent =
                        'Permission was denied. Re-enable it in your browser\'s site settings and try again.';
                    break;
                case 'default':
                    notificationsStatus.textContent =
                        'Permission prompt was dismissed. Toggle again and accept the prompt to enable.';
                    break;
                case 'unsupported':
                    notificationsStatus.textContent =
                        'Your browser does not support the Notification API. Toasts + sound alerts will still fire.';
                    break;
                case 'error':
                default:
                    notificationsStatus.textContent =
                        'Could not request permission. Toasts + sound alerts will still fire.';
                    break;
            }
        });
    }

    // ─── Push notification channels (server-side 24/7) ──────────────

    const channelsDivider = el('div');
    channelsDivider.style.borderTop = '1px solid var(--border-subtle)';
    channelsDivider.style.marginTop = 'var(--space-5)';
    channelsDivider.style.paddingTop = 'var(--space-4)';

    const channelsTitle = el('h3', null, 'Push notification channels');
    channelsTitle.style.margin = '0 0 var(--space-1) 0';
    const channelsSubtitle = el('div', 'subtitle',
        'Server-side notifications fire 24/7 even when the browser is closed. Changes save automatically.');
    channelsSubtitle.style.marginBottom = 'var(--space-4)';
    channelsDivider.append(channelsTitle, channelsSubtitle);
    panel.append(channelsDivider);

    const ch = (a.channels || {});

    // Poll interval
    const pollInput = numberInput('poll_interval_seconds',
        a.poll_interval_seconds || 30, 5, 300);
    bindNumberChange(pollInput, v => ({ alerts: { poll_interval_seconds: v } }));
    panel.append(field('alerts-poll-interval', 'Poll interval (seconds)', pollInput,
        'How often the server checks GPU metrics against thresholds. Lower values mean faster detection but more DB reads. Default 30s is a good balance.'));

    const instanceInput = textInput('instance_name', a.instance_name || '', 'e.g. ML-Rig-01');
    bindTextBlur(instanceInput, v => ({ alerts: { instance_name: v } }));
    panel.append(field('alerts-instance-name', 'Instance name', instanceInput,
        'Optional identifier for this GPU Monitor instance. Replaces "GPU Monitor" in notification titles so you can distinguish alerts from multiple machines reporting to the same channel. Leave empty for the default "GPU Monitor Alert" title.'));

    // Helper: build a test button for a channel
    function channelTestButton(channelName) {
        const wrap = el('div');
        wrap.style.display = 'flex';
        wrap.style.alignItems = 'center';
        wrap.style.gap = 'var(--space-2)';
        wrap.style.marginBottom = 'var(--space-3)';

        const btn = el('button', 'small', 'Test');
        btn.type = 'button';
        const status = el('span');
        status.style.fontSize = 'var(--font-size-sm)';

        btn.addEventListener('click', async () => {
            btn.disabled = true;
            btn.textContent = 'Testing…';
            status.textContent = '';
            try {
                const resp = await fetch(`/api/alerts/test/${channelName}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                });
                const data = await resp.json();
                if (data.ok) {
                    status.textContent = 'Test sent!';
                    status.style.color = 'var(--success)';
                } else {
                    status.textContent = data.error || 'Test failed';
                    status.style.color = 'var(--danger)';
                }
            } catch (err) {
                status.textContent = `Error: ${err.message}`;
                status.style.color = 'var(--danger)';
            } finally {
                btn.disabled = false;
                btn.textContent = 'Test';
            }
        });

        wrap.append(btn, status);
        return wrap;
    }

    // ── ntfy.sh ──
    const ntfySectionTitle = el('div', null, 'ntfy.sh');
    ntfySectionTitle.style.fontWeight = 'var(--font-weight-semibold)';
    ntfySectionTitle.style.marginTop = 'var(--space-4)';
    ntfySectionTitle.style.marginBottom = 'var(--space-2)';
    panel.append(ntfySectionTitle);

    const ntfy = ch.ntfy || {};
    panel.append(checkboxRow('alerts-ntfy-enabled', 'Enabled', ntfy.enabled,
        'Send push notifications via ntfy.sh when alert thresholds are breached.'));
    bindCheckboxChange(panel.querySelector('#alerts-ntfy-enabled'),
        v => ({ alerts: { channels: { ntfy: { enabled: v } } } }));

    const ntfyUrl = textInput('ntfy_topic_url', ntfy.topic_url || '', 'https://ntfy.sh/my-gpu-alerts');
    bindTextBlur(ntfyUrl, v => ({ alerts: { channels: { ntfy: { topic_url: v } } } }));
    panel.append(field('alerts-ntfy-url', 'Topic URL', ntfyUrl,
        'Full ntfy topic URL. Create a topic at ntfy.sh and paste the URL here.'));

    const ntfyPriority = selectInput('ntfy_priority', ntfy.priority || 'high', [
        { value: 'min',     label: 'Min' },
        { value: 'low',     label: 'Low' },
        { value: 'default', label: 'Default' },
        { value: 'high',    label: 'High' },
        { value: 'urgent',  label: 'Urgent' },
    ]);
    bindSelectChange(ntfyPriority, v => ({ alerts: { channels: { ntfy: { priority: v } } } }));
    panel.append(field('alerts-ntfy-priority', 'Priority', ntfyPriority));

    const ntfyToken = passwordInput('ntfy_token',
        ntfy.token_set ? '•••• (set)' : 'Access token (optional)');
    ntfyToken.addEventListener('blur', async () => {
        if (!ntfyToken.value) return;
        try {
            await autosave({ alerts: { channels: { ntfy: { token: ntfyToken.value } } } });
            ntfyToken.value = '';
            ntfyToken.placeholder = '•••• (set)';
        } catch { /* toast handles error */ }
    });
    panel.append(field('alerts-ntfy-token', 'Access token', ntfyToken,
        'Token for ntfy authentication (Bearer auth). Required for self-hosted instances with ACLs or private ntfy.sh cloud topics. Leave empty for public topics. Encrypted at rest.'));
    panel.append(channelTestButton('ntfy'));

    // ── Pushover ──
    const pushoverTitle = el('div', null, 'Pushover');
    pushoverTitle.style.fontWeight = 'var(--font-weight-semibold)';
    pushoverTitle.style.marginTop = 'var(--space-4)';
    pushoverTitle.style.marginBottom = 'var(--space-2)';
    panel.append(pushoverTitle);

    const pushover = ch.pushover || {};
    panel.append(checkboxRow('alerts-pushover-enabled', 'Enabled', pushover.enabled,
        'Send push notifications via Pushover when alert thresholds are breached.'));
    bindCheckboxChange(panel.querySelector('#alerts-pushover-enabled'),
        v => ({ alerts: { channels: { pushover: { enabled: v } } } }));

    const poUserKey = passwordInput('pushover_user_key',
        pushover.user_key_set ? '•••• (set)' : 'User Key');
    poUserKey.addEventListener('blur', async () => {
        if (!poUserKey.value) return;
        try {
            await autosave({ alerts: { channels: { pushover: { user_key: poUserKey.value } } } });
            poUserKey.value = '';
            poUserKey.placeholder = '•••• (set)';
        } catch { /* toast handles error */ }
    });
    panel.append(field('alerts-pushover-userkey', 'User Key', poUserKey,
        'Your Pushover user key. Find it at pushover.net/dashboard.'));

    const poAppToken = passwordInput('pushover_app_token',
        pushover.app_token_set ? '•••• (set)' : 'App Token');
    poAppToken.addEventListener('blur', async () => {
        if (!poAppToken.value) return;
        try {
            await autosave({ alerts: { channels: { pushover: { app_token: poAppToken.value } } } });
            poAppToken.value = '';
            poAppToken.placeholder = '•••• (set)';
        } catch { /* toast handles error */ }
    });
    panel.append(field('alerts-pushover-apptoken', 'App Token', poAppToken,
        'Your Pushover application token. Create an app at pushover.net/apps.'));
    panel.append(channelTestButton('pushover'));

    // ── Generic webhook ──
    const webhookTitle = el('div', null, 'Webhook');
    webhookTitle.style.fontWeight = 'var(--font-weight-semibold)';
    webhookTitle.style.marginTop = 'var(--space-4)';
    webhookTitle.style.marginBottom = 'var(--space-2)';
    panel.append(webhookTitle);

    const webhook = ch.webhook || {};
    panel.append(checkboxRow('alerts-webhook-enabled', 'Enabled', webhook.enabled,
        'Send alerts to a custom HTTP endpoint.'));
    bindCheckboxChange(panel.querySelector('#alerts-webhook-enabled'),
        v => ({ alerts: { channels: { webhook: { enabled: v } } } }));

    const webhookUrl = textInput('webhook_url', webhook.url || '', 'https://example.com/webhook');
    bindTextBlur(webhookUrl, v => ({ alerts: { channels: { webhook: { url: v } } } }));
    panel.append(field('alerts-webhook-url', 'URL', webhookUrl));

    const webhookMethod = selectInput('webhook_method', webhook.method || 'POST', [
        { value: 'POST', label: 'POST' },
        { value: 'PUT',  label: 'PUT' },
    ]);
    bindSelectChange(webhookMethod, v => ({ alerts: { channels: { webhook: { method: v } } } }));
    panel.append(field('alerts-webhook-method', 'Method', webhookMethod));

    const webhookHeaders = document.createElement('textarea');
    webhookHeaders.name = 'webhook_headers';
    webhookHeaders.rows = 3;
    webhookHeaders.placeholder = '{"Authorization": "Bearer ..."}';
    webhookHeaders.value = Object.keys(webhook.headers || {}).length
        ? JSON.stringify(webhook.headers, null, 2) : '';
    webhookHeaders.addEventListener('blur', () => {
        try {
            const parsed = webhookHeaders.value.trim()
                ? JSON.parse(webhookHeaders.value) : {};
            autosave({ alerts: { channels: { webhook: { headers: parsed } } } })
                .catch(() => {});
        } catch {
            showToast('Invalid JSON in headers', 'error');
        }
    });
    panel.append(field('alerts-webhook-headers', 'Custom headers (JSON)', webhookHeaders,
        'Optional JSON object of HTTP headers. Use for authentication, content-type overrides, etc.'));

    const webhookTemplate = document.createElement('textarea');
    webhookTemplate.name = 'webhook_body_template';
    webhookTemplate.rows = 3;
    webhookTemplate.placeholder = 'Leave empty for default JSON. Use {{gpu_name}}, {{metric}}, {{value}}, {{threshold}}, {{message}}, {{timestamp}}';
    webhookTemplate.value = webhook.body_template || '';
    webhookTemplate.addEventListener('blur', () => {
        autosave({ alerts: { channels: { webhook: { body_template: webhookTemplate.value } } } })
            .catch(() => {});
    });
    panel.append(field('alerts-webhook-template', 'Body template', webhookTemplate,
        'Custom body template with {{key}} placeholders. Leave empty to send default JSON payload with all alert fields.'));

    const webhookAuth = passwordInput('webhook_auth_token',
        webhook.auth_token_set ? '•••• (set)' : 'Bearer token');
    webhookAuth.addEventListener('blur', async () => {
        if (!webhookAuth.value) return;
        try {
            await autosave({ alerts: { channels: { webhook: { auth_token: webhookAuth.value } } } });
            webhookAuth.value = '';
            webhookAuth.placeholder = '•••• (set)';
        } catch { /* toast */ }
    });
    panel.append(field('alerts-webhook-auth', 'Auth token (optional)', webhookAuth,
        'If set, added as Authorization: Bearer header. Encrypted at rest.'));
    panel.append(channelTestButton('webhook'));

    // ── Email alerts ──
    const emailTitle = el('div', null, 'Email alerts');
    emailTitle.style.fontWeight = 'var(--font-weight-semibold)';
    emailTitle.style.marginTop = 'var(--space-4)';
    emailTitle.style.marginBottom = 'var(--space-2)';
    panel.append(emailTitle);

    const emailCh = ch.email || {};
    panel.append(checkboxRow('alerts-email-enabled', 'Enabled', emailCh.enabled,
        'Send short alert emails when thresholds are breached. Uses SMTP config from the SMTP tab.'));
    bindCheckboxChange(panel.querySelector('#alerts-email-enabled'),
        v => ({ alerts: { channels: { email: { enabled: v } } } }));

    const emailRecipients = textInput('email_alert_recipients',
        (emailCh.recipients || []).join(', '), 'admin@example.com, ops@example.com');
    emailRecipients.addEventListener('blur', () => {
        const list = emailRecipients.value.split(',').map(s => s.trim()).filter(Boolean);
        autosave({ alerts: { channels: { email: { recipients: list } } } })
            .catch(() => {});
    });
    panel.append(field('alerts-email-recipients', 'Recipients (comma-separated)', emailRecipients,
        'Alert emails are sent to these addresses. Uses your SMTP configuration from the SMTP tab — no separate mail server setup needed.'));
    panel.append(channelTestButton('email'));

    return panel;
}

function renderPowerTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Power'));
    header.append(el('div', 'subtitle',
        'Electricity rate used for the Power view\'s cost calculation. Changes save automatically.'));
    panel.append(header);

    const p = state.settings.power || {};

    const rateInput = numberInput('rate_per_kwh', p.rate_per_kwh || 0, 0, 10, 0.0001);
    bindNumberChange(rateInput, v => ({ power: { rate_per_kwh: v } }));
    panel.append(field('power-rate', 'Rate per kWh', rateInput,
        'Your electricity tariff. Used to convert integrated Wh into a cost estimate on the Power view. Leave at 0 to hide cost displays.'));

    const currencyInput = textInput('currency', p.currency || '$', '$');
    currencyInput.maxLength = 4;
    bindTextBlur(currencyInput, v => ({ power: { currency: v } }));
    panel.append(field('power-currency', 'Currency symbol', currencyInput,
        'Single-character (or short) currency symbol displayed next to cost values. Examples: $ € £ ¥.'));

    return panel;
}

function renderLoggingTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Logging'));
    header.append(el('div', 'subtitle',
        'Log rotation thresholds. Rotation fires when EITHER threshold is exceeded. Changes save automatically.'));
    panel.append(header);

    const l = state.settings.logging || {};

    const sizeInput = numberInput('max_size_mb', l.max_size_mb, 1, 100);
    bindNumberChange(sizeInput, v => ({ logging: { max_size_mb: v } }));
    panel.append(field('log-size', 'Max size per log (MB)', sizeInput,
        'Rotated when any single log file exceeds this size.'));

    const ageInput = numberInput('max_age_hours', l.max_age_hours, 1, 720);
    bindNumberChange(ageInput, v => ({ logging: { max_age_hours: v } }));
    panel.append(field('log-age', 'Max age (hours)', ageInput,
        'Rotated when the log file is older than this. Default 25 h keeps the last day of logs plus a rollover margin.'));

    return panel;
}

function renderThemeTab() {
    const panel = el('section', 'card');
    const header = el('header');
    header.append(el('h3', null, 'Theme'));
    header.append(el('div', 'subtitle',
        'Default theme mode when the dashboard loads. Changes save automatically.'));
    panel.append(header);

    const t = state.settings.theme || {};

    const modeSelect = selectInput('default_mode', t.default_mode || 'auto', [
        { value: 'auto',  label: 'Auto (follow OS preference)' },
        { value: 'light', label: 'Light' },
        { value: 'dark',  label: 'Dark' },
    ]);
    bindSelectChange(modeSelect, v => ({ theme: { default_mode: v } }));
    panel.append(field('theme-default', 'Default mode', modeSelect,
        'The sidebar has a live toggle that overrides this per-session. This setting is the default on a fresh page load.'));

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

    // Retention (part of the settings form) — autosaves on change.
    const retentionInput = numberInput('retention_days', h.retention_days, 1, 365);
    bindNumberChange(retentionInput, v => ({
        housekeeping: { retention_days: v },
    }));
    panel.append(field('housekeeping-retention', 'Retention (days)', retentionInput,
        'The nightly clean_old_data sweep deletes rows older than this. Changes apply at the next midnight sweep.'));

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

    // The "Purge older than N days" input is the argument for the Purge
    // action button, not a server-side setting — the actual purge happens
    // when the user clicks Purge and deletes exactly N-days-old rows. But
    // because every other field in Settings autosaves on change, leaving
    // this one silent feels inconsistent ("why doesn't my number save?").
    //
    // The fix: persist the last value in localStorage so the field
    // remembers across sessions, and show a "Remembered" toast on change
    // so the user gets the same visual confirmation pattern as everywhere
    // else in Settings. The value never leaves the browser — no server
    // schema change, no new settings.json key.
    const PURGE_DAYS_KEY = 'gpu-monitor:purge-days';
    let initialPurgeDays = 7;
    try {
        const stored = Number(localStorage.getItem(PURGE_DAYS_KEY));
        if (Number.isFinite(stored) && stored >= 1 && stored <= 365) {
            initialPurgeDays = stored;
        }
    } catch {
        // localStorage can throw in some Safari modes. Fall back to 7.
    }
    const purgeInput = numberInput('purge_days', initialPurgeDays, 1, 365);
    purgeInput.style.width = '80px';
    purgeInput.addEventListener('change', () => {
        const v = numericValue(purgeInput);
        if (v === null) return;
        try {
            localStorage.setItem(PURGE_DAYS_KEY, String(v));
            showToast('Remembered', 'success');
        } catch {
            // localStorage write failed — silently drop. The input
            // still holds the user's value for the current page load,
            // so the button will work; only the cross-session memory
            // is lost.
        }
    });
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
        'Unique identifier used by run-now and the scheduler. Cannot be changed after creation.'));

    const templateSelect = selectInput('new_template', 'daily', [
        { value: 'daily',   label: 'Daily (last 24 hours)' },
        { value: 'weekly',  label: 'Weekly (last 7 days)' },
        { value: 'monthly', label: 'Monthly (last 30 days)' },
    ]);
    addWrap.append(field('report-new-template', 'Template', templateSelect,
        'Determines the data window (24h / 7d / 30d) and the default email subject line.'));

    const cronInput = textInput('new_cron', '0 8 * * *', '0 8 * * *');
    addWrap.append(field('report-new-cron', 'Cron expression', cronInput,
        'Standard 5-field cron. "0 8 * * *" = every day at 08:00. Evaluated in the container\'s TZ environment variable.'));

    const recipientsInput = textInput('new_recipients', '', 'a@example.com, b@example.com');
    addWrap.append(field('report-new-recipients', 'Recipients (comma-separated)', recipientsInput));

    // Subject field: auto-derived from the template selection as a
    // default, but editable. If the user clears it, the server falls
    // back to "GPU Monitor {template} report". The auto-population
    // fires on template change so switching from "daily" to "weekly"
    // updates the subject placeholder accordingly — but only if the
    // user hasn't typed a custom value.
    const subjectInput = textInput('new_subject', '', 'GPU Monitor daily report');
    let subjectManuallyEdited = false;
    subjectInput.addEventListener('input', () => { subjectManuallyEdited = true; });
    templateSelect.addEventListener('change', () => {
        if (!subjectManuallyEdited) {
            subjectInput.value = '';
            subjectInput.placeholder = `GPU Monitor ${templateSelect.value} report`;
        }
    });
    addWrap.append(field('report-new-subject', 'Email subject', subjectInput,
        'Custom email subject line. Leave empty to use the default "GPU Monitor {template} report". Changing the template auto-updates this unless you\'ve typed a custom value.'));

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
                subject: subjectInput.value.trim() || null,
                enabled: true,
                last_run_epoch: null,
            },
        ];
        addBtn.disabled = true;
        addBtn.textContent = 'Adding…';
        addStatus.textContent = '';
        try {
            await api.putSettings({ schedules: nextSchedules });
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

    // ─── Read-only view (default) ──────────────────────────────────
    const readView = el('div');
    readView.style.display = 'flex';
    readView.style.flexDirection = 'column';
    readView.style.gap = 'var(--space-2)';

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
    readView.append(topRow);

    // Show the subject if it's custom; if null/empty, show the
    // auto-derived default in a muted style so the user can see
    // what the email will say.
    const subjectLine = el('div');
    subjectLine.style.fontSize = 'var(--font-size-sm)';
    subjectLine.style.color = schedule.subject
        ? 'var(--text-secondary)'
        : 'var(--text-tertiary)';
    subjectLine.textContent = 'Subject: ' +
        (schedule.subject || `GPU Monitor ${schedule.template} report`);
    if (!schedule.subject) {
        subjectLine.textContent += ' (default)';
    }
    readView.append(subjectLine);

    const recipientsLine = el('div');
    recipientsLine.style.fontSize = 'var(--font-size-sm)';
    recipientsLine.style.color = 'var(--text-secondary)';
    recipientsLine.textContent = 'To: ' + (schedule.recipients || []).join(', ');
    readView.append(recipientsLine);

    if (schedule.last_run_epoch) {
        const lastRun = el('div');
        lastRun.style.fontSize = 'var(--font-size-sm)';
        lastRun.style.color = 'var(--text-tertiary)';
        const d = new Date(schedule.last_run_epoch * 1000);
        lastRun.textContent = 'Last run: ' + d.toLocaleString();
        readView.append(lastRun);
    }

    // Action buttons (read mode)
    const actionRow = el('div');
    actionRow.style.display = 'flex';
    actionRow.style.gap = 'var(--space-2)';
    actionRow.style.marginTop = 'var(--space-2)';

    const runBtn = el('button', 'small', 'Run now');
    runBtn.type = 'button';
    const editBtn = el('button', 'small', 'Edit');
    editBtn.type = 'button';
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

    actionRow.append(runBtn, editBtn, removeBtn, runStatus);
    readView.append(actionRow);
    card.append(readView);

    // ─── Inline edit view (hidden by default) ──────────────────────
    const editView = el('div');
    editView.style.display = 'none';
    editView.style.flexDirection = 'column';
    editView.style.gap = 'var(--space-2)';

    const editTitle = el('div');
    editTitle.style.fontWeight = 'var(--font-weight-semibold)';
    editTitle.style.marginBottom = 'var(--space-2)';
    editTitle.textContent = `Edit: ${schedule.id}`;
    editView.append(editTitle);

    const editTemplateSelect = selectInput(`edit_template_${schedule.id}`, schedule.template, [
        { value: 'daily',   label: 'Daily (last 24 hours)' },
        { value: 'weekly',  label: 'Weekly (last 7 days)' },
        { value: 'monthly', label: 'Monthly (last 30 days)' },
    ]);
    editView.append(field(`edit-template-${schedule.id}`, 'Template', editTemplateSelect));

    const editCronInput = textInput(`edit_cron_${schedule.id}`, schedule.cron, '0 8 * * *');
    editView.append(field(`edit-cron-${schedule.id}`, 'Cron expression', editCronInput));

    const editRecipientsInput = textInput(
        `edit_recipients_${schedule.id}`,
        (schedule.recipients || []).join(', '),
        'a@example.com, b@example.com',
    );
    editView.append(field(`edit-recipients-${schedule.id}`, 'Recipients (comma-separated)', editRecipientsInput));

    const editSubjectInput = textInput(
        `edit_subject_${schedule.id}`,
        schedule.subject || '',
        `GPU Monitor ${schedule.template} report`,
    );
    // Update placeholder when template changes (mirrors the add-form
    // behavior) so the user sees what the default subject will be.
    editTemplateSelect.addEventListener('change', () => {
        editSubjectInput.placeholder = `GPU Monitor ${editTemplateSelect.value} report`;
    });
    editView.append(field(`edit-subject-${schedule.id}`, 'Email subject', editSubjectInput,
        'Leave empty to use the default "GPU Monitor {template} report".'));

    const editBtnRow = el('div');
    editBtnRow.style.display = 'flex';
    editBtnRow.style.gap = 'var(--space-2)';
    editBtnRow.style.marginTop = 'var(--space-2)';

    const saveEditBtn = el('button', 'primary small', 'Save');
    saveEditBtn.type = 'button';
    const cancelEditBtn = el('button', 'small', 'Cancel');
    cancelEditBtn.type = 'button';
    const editStatus = el('span');
    editStatus.style.fontSize = 'var(--font-size-sm)';
    editStatus.style.marginLeft = 'var(--space-2)';

    saveEditBtn.addEventListener('click', async () => {
        const newRecipients = editRecipientsInput.value
            .split(',')
            .map(s => s.trim())
            .filter(Boolean);
        if (newRecipients.length === 0) {
            editStatus.textContent = 'At least one recipient is required.';
            editStatus.style.color = 'var(--danger)';
            return;
        }
        const current = Array.isArray(state.settings.schedules)
            ? state.settings.schedules
            : [];
        const updated = current.map(s => {
            if (s.id !== schedule.id) return s;
            return {
                ...s,
                template: editTemplateSelect.value,
                cron: editCronInput.value,
                recipients: newRecipients,
                subject: editSubjectInput.value.trim() || null,
            };
        });
        saveEditBtn.disabled = true;
        saveEditBtn.textContent = 'Saving…';
        editStatus.textContent = '';
        try {
            await api.putSettings({ schedules: updated });
            await reloadAndReRender();
        } catch (err) {
            editStatus.textContent = `Save failed: ${err.message}`;
            editStatus.style.color = 'var(--danger)';
        } finally {
            saveEditBtn.disabled = false;
            saveEditBtn.textContent = 'Save';
        }
    });

    cancelEditBtn.addEventListener('click', () => {
        editView.style.display = 'none';
        readView.style.display = 'flex';
    });

    editBtnRow.append(saveEditBtn, cancelEditBtn, editStatus);
    editView.append(editBtnRow);
    card.append(editView);

    // ─── Toggle between read and edit views ────────────────────────
    editBtn.addEventListener('click', () => {
        readView.style.display = 'none';
        editView.style.display = 'flex';
    });

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
