/*
 * widgets/tablist.js — WAI-ARIA tab pattern helpers.
 *
 * Phase 7. Implements the tablist-keyboard pattern that was deferred
 * from Phase 4 round 3 as task #28. The four tab strips in the app
 * (GPU picker in dashboard.js, time-range picker in dashboard.js,
 * GPU picker in power.js, window picker in power.js) all share the
 * same pattern, so factoring the accessibility boilerplate into one
 * place avoids copy-paste drift.
 *
 * The WAI-ARIA tab pattern requires:
 *   1. Container has role="tablist"
 *   2. Each tab has role="tab" + aria-selected="true|false"
 *   3. Roving tabindex — only the active tab has tabindex="0";
 *      inactive tabs have tabindex="-1" so they're reachable by
 *      click but NOT by sequential Tab navigation
 *   4. Arrow keys move focus between tabs (Home/End → first/last)
 *
 * Call sites already use `aria-current` to mark the active tab
 * from Phase 4. This module:
 *   * Replaces aria-current with aria-selected (the correct ARIA
 *     attribute for tabs — aria-current is for navigation links)
 *   * Adds the roving tabindex management
 *   * Adds arrow-key handling via a single addEventListener on
 *     the tablist container
 *
 * Usage:
 *   const tablist = document.createElement('div');
 *   tablist.className = 'tabs';
 *   tablist.setAttribute('role', 'tablist');
 *   // ... append role="tab" buttons ...
 *   attachTablistKeyboard(tablist, { onSelect });
 *   markTabSelected(tablist, activeTabElement);
 *
 * Preserves the existing aria-current behavior for back-compat
 * with CSS selectors that still target aria-current="true" (see
 * components.css line 133 → .tabs button[aria-current="true"]).
 * Dropping aria-current would break the tab highlight styling
 * across the entire app, so we dual-stamp both attributes.
 */

export function markTabSelected(tablist, selectedTab) {
    // Dual-stamp: aria-selected is the correct tab-pattern
    // attribute for assistive technology, aria-current is the
    // legacy attribute our CSS highlights against. Both are
    // needed until a separate CSS polish pass migrates the
    // selector.
    //
    // Roving tabindex: the selected tab gets tabindex="0" (in the
    // sequential Tab order), all other tabs get tabindex="-1"
    // (focusable by click / arrow key but skipped by Tab).
    const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
    tabs.forEach(tab => {
        const isSelected = tab === selectedTab;
        if (isSelected) {
            tab.setAttribute('aria-selected', 'true');
            tab.setAttribute('aria-current', 'true');
            tab.setAttribute('tabindex', '0');
        } else {
            tab.setAttribute('aria-selected', 'false');
            tab.removeAttribute('aria-current');
            tab.setAttribute('tabindex', '-1');
        }
    });
}

export function attachTablistKeyboard(tablist, { onSelect } = {}) {
    // Single keydown listener on the container — handlers on
    // individual tabs would be redundant and harder to remove on
    // view unmount.
    const handler = (event) => {
        const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
        if (tabs.length === 0) return;
        const currentIndex = tabs.indexOf(document.activeElement);
        if (currentIndex === -1) return;

        let targetIndex = null;
        switch (event.key) {
            case 'ArrowRight':
            case 'ArrowDown':
                targetIndex = (currentIndex + 1) % tabs.length;
                break;
            case 'ArrowLeft':
            case 'ArrowUp':
                targetIndex = (currentIndex - 1 + tabs.length) % tabs.length;
                break;
            case 'Home':
                targetIndex = 0;
                break;
            case 'End':
                targetIndex = tabs.length - 1;
                break;
            default:
                return;
        }

        event.preventDefault();
        const targetTab = tabs[targetIndex];
        targetTab.focus();
        markTabSelected(tablist, targetTab);

        // WAI-ARIA "automatic activation" tab pattern: focus
        // implies activation. If the caller provides an onSelect
        // callback, fire it with the target tab so the view can
        // refresh its content for the newly-selected tab.
        // "Manual activation" (where the user presses Space/Enter
        // after arrow-focusing) is the alternative pattern, but
        // our tabs mutate display state (selected GPU, time range)
        // that should follow focus for immediate feedback. Both
        // patterns are valid per WAI-ARIA.
        if (typeof onSelect === 'function') {
            onSelect(targetTab);
        }
    };

    tablist.addEventListener('keydown', handler);
    // Return a detach function for symmetric cleanup — views should
    // call this in unmount() if they retain a reference to the
    // tablist, but for the current views the tablist dies with the
    // container so explicit cleanup is optional.
    return () => tablist.removeEventListener('keydown', handler);
}
