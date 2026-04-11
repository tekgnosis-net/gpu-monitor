/*
 * keybindings.js — Global keyboard shortcuts.
 *
 * Phase 7. Installs a single window-level keydown listener that
 * handles the documented shortcut set:
 *
 *   g d     — navigate to Dashboard
 *   g r     — navigate to Report
 *   g p     — navigate to Power
 *   g s     — navigate to Settings
 *   t       — toggle theme (auto → light → dark → auto)
 *   \       — toggle sidebar collapse
 *   ?       — show / hide the keyboard cheat-sheet overlay
 *   Escape  — close the cheat-sheet overlay if visible
 *
 * The "g <key>" navigation prefix mirrors the convention
 * established by Gmail and GitHub: press `g`, release, then
 * press the destination key within a short window. This
 * two-key combo avoids colliding with single-key shortcuts
 * like `t` (theme) that are common across the app.
 *
 * Shortcuts are INTENTIONALLY SUPPRESSED when the user is
 * typing in an input or textarea. Without that guard, pressing
 * `t` while editing the SMTP hostname would toggle the theme
 * mid-keystroke. The suppression checks:
 *
 *   * event target is <input>, <textarea>, or <select>, or is
 *     contenteditable
 *   * event has a modifier key (Ctrl/Alt/Meta) — reserved for
 *     browser and OS shortcuts
 *
 * Alphabetic key comparisons are case-INSENSITIVE — a user
 * holding Shift or with Caps Lock on should still be able to
 * press `g d` to navigate to Dashboard. We normalize via
 * event.key.toLowerCase() before comparing so `'g'` and `'G'`
 * both arm the navigation prefix, `'t'` and `'T'` both cycle
 * the theme, etc.
 *
 * Focus management: the cheat-sheet overlay is a real
 * role="dialog" aria-modal="true". On show, we save the
 * previously-focused element and move focus into the overlay
 * (a close button with autofocus). On hide, we restore focus
 * to the saved element. A full Tab-focus trap is intentionally
 * not implemented — the overlay is a read-only list with a
 * single close affordance, so Tab-trapping would add complexity
 * without changing the practical keyboard flow.
 *
 * Safe-DOM: the cheat-sheet overlay is built with createElement
 * + textContent — no innerHTML with dynamic content.
 */

import { navigate } from './router.js';
import { cycleTheme } from './theme.js';
import { toggleSidebar } from './sidebar.js';

// Navigation prefix: true for ~1000 ms after pressing 'g'
let navigationArmed = false;
let navigationTimer = null;

// Reference to the cheat-sheet overlay when visible; null otherwise
let cheatSheetEl = null;

// The element that had focus when the cheat sheet was opened, so
// we can restore focus on close. Saving this up-front and
// restoring it in hideCheatSheet() satisfies the WAI-ARIA dialog
// pattern's focus-management expectation without implementing a
// full Tab-focus trap (see the module header for rationale).
let previouslyFocusedEl = null;


function isEditingFormControl(event) {
    const target = event.target;
    if (!target || !target.tagName) return false;

    const tag = target.tagName.toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;

    // contenteditable elements (div, span, etc.)
    if (target.isContentEditable) return true;

    return false;
}


function armNavigationPrefix() {
    navigationArmed = true;
    if (navigationTimer) clearTimeout(navigationTimer);
    // 1000 ms window for the second key. Gmail uses ~2s; 1s is
    // tighter but still comfortable for the common case of a
    // deliberate two-key sequence.
    navigationTimer = setTimeout(() => {
        navigationArmed = false;
        navigationTimer = null;
    }, 1000);
}


function consumeNavigationPrefix() {
    navigationArmed = false;
    if (navigationTimer) {
        clearTimeout(navigationTimer);
        navigationTimer = null;
    }
}


/* ─── Cheat-sheet overlay ──────────────────────────────────────────────── */

const SHORTCUT_ROWS = [
    { keys: ['g', 'd'],  label: 'Go to Dashboard' },
    { keys: ['g', 'r'],  label: 'Go to Report' },
    { keys: ['g', 'p'],  label: 'Go to Power' },
    { keys: ['g', 's'],  label: 'Go to Settings' },
    { keys: ['t'],       label: 'Toggle theme (auto / light / dark)' },
    { keys: ['\\'],      label: 'Toggle sidebar collapse' },
    { keys: ['?'],       label: 'Show / hide this shortcut list' },
    { keys: ['Escape'],  label: 'Close overlay' },
];


function buildCheatSheet() {
    const overlay = document.createElement('div');
    overlay.id = 'keyboard-cheat-sheet';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', 'Keyboard shortcuts');
    overlay.style.position = 'fixed';
    overlay.style.inset = '0';
    overlay.style.background = 'var(--bg-overlay, rgba(0,0,0,0.4))';
    overlay.style.zIndex = '2000';
    overlay.style.display = 'flex';
    overlay.style.alignItems = 'center';
    overlay.style.justifyContent = 'center';
    overlay.style.padding = 'var(--space-5, 32px)';

    const card = document.createElement('div');
    card.style.background = 'var(--bg-secondary, #fff)';
    card.style.color = 'var(--text-primary, #1d1d1f)';
    card.style.border = '1px solid var(--border-regular, rgba(60,60,67,0.18))';
    card.style.borderRadius = 'var(--radius-lg, 14px)';
    card.style.padding = 'var(--space-5, 32px)';
    card.style.boxShadow = 'var(--shadow-lg, 0 10px 30px rgba(0,0,0,0.12))';
    card.style.maxWidth = '480px';
    card.style.width = '100%';

    const title = document.createElement('h2');
    title.textContent = 'Keyboard shortcuts';
    title.style.margin = '0 0 var(--space-4, 16px) 0';
    title.style.fontSize = 'var(--font-size-xl, 22px)';
    title.style.fontWeight = 'var(--font-weight-semibold, 600)';
    card.append(title);

    const list = document.createElement('div');
    list.style.display = 'grid';
    list.style.gridTemplateColumns = 'auto 1fr';
    list.style.rowGap = 'var(--space-3, 12px)';
    list.style.columnGap = 'var(--space-4, 16px)';
    list.style.fontSize = 'var(--font-size-md, 15px)';

    SHORTCUT_ROWS.forEach(row => {
        const keys = document.createElement('div');
        keys.style.fontFamily = 'var(--font-mono, monospace)';
        keys.style.color = 'var(--text-secondary, #3c3c43)';

        row.keys.forEach((key, i) => {
            if (i > 0) {
                const sep = document.createElement('span');
                sep.textContent = ' ';
                keys.append(sep);
            }
            const kbd = document.createElement('kbd');
            kbd.textContent = key;
            kbd.style.background = 'var(--bg-tertiary, #fafafa)';
            kbd.style.border = '1px solid var(--border-subtle, rgba(60,60,67,0.1))';
            kbd.style.borderRadius = 'var(--radius-sm, 6px)';
            kbd.style.padding = '2px 8px';
            kbd.style.fontSize = 'var(--font-size-sm, 13px)';
            keys.append(kbd);
        });

        const label = document.createElement('div');
        label.textContent = row.label;
        label.style.color = 'var(--text-primary, #1d1d1f)';

        list.append(keys, label);
    });

    card.append(list);

    // Close button — also serves as the focus target on show so
    // Tab / Shift+Tab / Escape all have a sensible starting point
    // for keyboard users. WAI-ARIA dialog guidance wants focus to
    // land inside the dialog when it opens, and an explicit
    // Close affordance is clearer than relying on ? or Escape.
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.textContent = 'Close';
    closeBtn.style.marginTop = 'var(--space-4, 16px)';
    closeBtn.style.display = 'block';
    closeBtn.style.marginLeft = 'auto';
    closeBtn.style.marginRight = 'auto';
    closeBtn.addEventListener('click', hideCheatSheet);
    card.append(closeBtn);

    const hint = document.createElement('div');
    hint.textContent = 'Press ? or Escape to close';
    hint.style.marginTop = 'var(--space-3, 12px)';
    hint.style.fontSize = 'var(--font-size-sm, 13px)';
    hint.style.color = 'var(--text-tertiary, #6e6e73)';
    hint.style.textAlign = 'center';
    card.append(hint);

    overlay.append(card);

    // Click on the backdrop (not the card) closes the overlay
    overlay.addEventListener('click', (event) => {
        if (event.target === overlay) hideCheatSheet();
    });

    // Stash the close button on the overlay so showCheatSheet()
    // can focus it without re-querying. Using a non-enumerable
    // property to avoid polluting the element's standard shape.
    Object.defineProperty(overlay, '_closeButton', {
        value: closeBtn,
        enumerable: false,
    });

    return overlay;
}


function showCheatSheet() {
    if (cheatSheetEl) return;
    // Save the currently-focused element BEFORE we mutate the DOM
    // so we can restore focus on close. If nothing is focused or
    // document.body has focus (which browsers report when no
    // real control is focused), previouslyFocusedEl stays null
    // and we'll skip the restore call.
    const active = document.activeElement;
    previouslyFocusedEl = (active && active !== document.body) ? active : null;

    cheatSheetEl = buildCheatSheet();
    document.body.append(cheatSheetEl);

    // Move focus into the dialog. The close button is the most
    // useful focus target because Tab/Shift+Tab cycle naturally
    // around the surrounding page (we intentionally don't trap
    // focus — see the module header for rationale) but Enter/
    // Space on it triggers dismissal.
    const closeBtn = cheatSheetEl._closeButton;
    if (closeBtn && typeof closeBtn.focus === 'function') {
        // requestAnimationFrame ensures the focus() call happens
        // after the element is actually inserted into layout.
        // Firing focus() inside the same microtask as append()
        // can no-op on some browsers.
        requestAnimationFrame(() => closeBtn.focus());
    }
}


function hideCheatSheet() {
    if (!cheatSheetEl) return;
    cheatSheetEl.remove();
    cheatSheetEl = null;

    // Restore focus to whatever had it before the dialog opened.
    // If the saved element has since been removed from the DOM
    // (unlikely in this app but possible during hot-reload),
    // focus() throws silently and we move on.
    if (previouslyFocusedEl && typeof previouslyFocusedEl.focus === 'function') {
        try {
            previouslyFocusedEl.focus();
        } catch { /* element was removed from the DOM — no-op */ }
    }
    previouslyFocusedEl = null;
}


/* ─── Main handler ─────────────────────────────────────────────────────── */

function handleKeyDown(event) {
    // Suppress all shortcuts while the user is typing in an input
    // or textarea. The Escape key is a special case — it should
    // close the cheat-sheet overlay even from inside an input —
    // but we only handle Escape when the overlay is actually open.
    if (event.key === 'Escape' && cheatSheetEl) {
        hideCheatSheet();
        event.preventDefault();
        return;
    }

    if (isEditingFormControl(event)) return;

    // Modifier-key combos are reserved for browser/OS shortcuts.
    if (event.ctrlKey || event.altKey || event.metaKey) return;

    // Case-insensitive compare for alphabetic keys so Caps Lock
    // and Shift don't break the shortcut set. `event.key` is
    // already lowercase for punctuation and special keys (`\`,
    // `?`, `Escape`) so the toLowerCase() is a no-op there but
    // correctly normalizes `G` → `g`, `T` → `t`, etc.
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;

    // Navigation prefix: `g` arms the prefix. The next keypress
    // within 1000 ms interprets as a navigation target.
    if (navigationArmed) {
        consumeNavigationPrefix();
        switch (key) {
            case 'd': navigate('/dashboard'); event.preventDefault(); return;
            case 'r': navigate('/report');    event.preventDefault(); return;
            case 'p': navigate('/power');     event.preventDefault(); return;
            case 's': navigate('/settings');  event.preventDefault(); return;
            default:
                // Unknown target → do nothing, don't interpret as
                // a single-key shortcut either (the user just
                // pressed `g x` where x is noise).
                return;
        }
    }

    switch (key) {
        case 'g':
            armNavigationPrefix();
            event.preventDefault();
            return;
        case 't':
            cycleTheme();
            event.preventDefault();
            return;
        case '\\':
            toggleSidebar();
            event.preventDefault();
            return;
        case '?':
            if (cheatSheetEl) hideCheatSheet();
            else showCheatSheet();
            event.preventDefault();
            return;
        default:
            return;
    }
}


export function installKeybindings() {
    window.addEventListener('keydown', handleKeyDown);
    // Return a detach function in case the caller wants to
    // uninstall the handler on teardown. In practice the
    // bindings live for the page lifetime.
    return () => window.removeEventListener('keydown', handleKeyDown);
}
