/*
 * widgets/toast.js — Floating toast notifications.
 *
 * Used by the Settings view's autosave flow to signal success and
 * failure without interrupting the user. The toast floats in the
 * bottom-right corner of the viewport, auto-dismisses after 2000ms,
 * and stacks when multiple fire in quick succession.
 *
 * Design decisions:
 *
 *   1. One mounted container per document. The container is lazily
 *      created on first call, positioned with `position: fixed`, and
 *      reused across subsequent calls. Toasts within it are absolute-
 *      positioned siblings that stack via CSS flex column.
 *
 *   2. No dependency on Lit or any framework. Toasts are small enough
 *      that hand-rolling DOM + inline styles is cheaper than adding
 *      a component system. Matches the pattern used by the Settings
 *      view's field builders.
 *
 *   3. Variants are encoded as a modifier class (`.toast--success`,
 *      `.toast--error`) which maps to CSS custom properties that the
 *      dashboard's theme tokens already define. Success uses
 *      --success, error uses --danger — both present in tokens.css.
 *
 *   4. Animations use CSS transitions rather than keyframes. The
 *      toast starts at opacity 0 translated down, then transitions
 *      to opacity 1 / translate 0 on the next frame, then back on
 *      dismiss. `transition` is the cheapest way to get both enter
 *      and exit animations in the same rule.
 *
 *   5. `prefers-reduced-motion` users get an instant show/hide —
 *      the transitions are gated on the media query so users who
 *      have opted out of animations don't see the slide.
 *
 *   6. Dismissal happens on a timer, not a click (yet). If the
 *      user wants click-to-dismiss, a single addEventListener call
 *      in the toast element creation block would add it — left
 *      as a TODO for now since autosave toasts are informational
 *      and rarely stack more than two deep.
 */

const DISMISS_MS = 2000;
const ANIMATION_MS = 200;

let container = null;

function ensureContainer() {
    if (container) return container;
    container = document.createElement('div');
    container.id = 'toast-container';
    container.setAttribute('aria-live', 'polite');
    container.setAttribute('aria-atomic', 'false');
    container.style.position = 'fixed';
    container.style.bottom = 'var(--space-5, 24px)';
    container.style.right = 'var(--space-5, 24px)';
    container.style.display = 'flex';
    container.style.flexDirection = 'column';
    container.style.gap = 'var(--space-2, 8px)';
    container.style.zIndex = '9999';
    container.style.pointerEvents = 'none';
    document.body.appendChild(container);
    return container;
}

/**
 * Show a toast notification.
 *
 * @param {string} message  Human-readable message. Never sanitized for
 *                          HTML — uses textContent, so any markup is
 *                          rendered as literal text.
 * @param {'success'|'error'} variant  Visual style. Maps to --success
 *                          or --danger color tokens.
 */
export function showToast(message, variant = 'success') {
    const host = ensureContainer();

    const toast = document.createElement('div');
    toast.setAttribute('role', variant === 'error' ? 'alert' : 'status');
    toast.textContent = message;

    // Visual styling driven by theme tokens + a variant class.
    toast.style.pointerEvents = 'auto';
    toast.style.padding = 'var(--space-3, 12px) var(--space-4, 16px)';
    toast.style.borderRadius = 'var(--radius-md, 8px)';
    toast.style.fontSize = 'var(--font-size-sm, 13px)';
    toast.style.fontWeight = 'var(--font-weight-medium, 500)';
    toast.style.boxShadow =
        '0 4px 12px rgba(0, 0, 0, 0.15), 0 1px 3px rgba(0, 0, 0, 0.08)';
    toast.style.maxWidth = '320px';
    toast.style.wordBreak = 'break-word';

    // Variant-specific background + text color. The dashboard's
    // tokens define --success / --danger and their contrasting
    // text colors — we use a solid background with white text for
    // maximum visibility rather than the subtler surface-on-tint
    // treatment the rest of the app uses.
    if (variant === 'error') {
        toast.style.background = 'var(--danger, #ff3b30)';
        toast.style.color = '#ffffff';
    } else {
        toast.style.background = 'var(--success, #34c759)';
        toast.style.color = '#ffffff';
    }

    // Animate in from below. The initial state is set inline so
    // the CSS transition has a "from" value; the class change in
    // requestAnimationFrame triggers the "to" transition.
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(8px)';
    toast.style.transition =
        `opacity ${ANIMATION_MS}ms ease-out, ` +
        `transform ${ANIMATION_MS}ms ease-out`;

    // Respect prefers-reduced-motion: skip the slide/fade entirely
    // and jump straight to the visible state.
    const reducedMotion =
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reducedMotion) {
        toast.style.transition = 'none';
    }

    host.appendChild(toast);

    // Next frame: animate to the visible state.
    requestAnimationFrame(() => {
        toast.style.opacity = '1';
        toast.style.transform = 'translateY(0)';
    });

    // Dismissal: animate out, then remove from DOM.
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(8px)';
        setTimeout(() => {
            toast.remove();
            // If the container is now empty, leave it in place —
            // removing/recreating it would cause a flash when the
            // next toast appears. It's a single empty div.
        }, reducedMotion ? 0 : ANIMATION_MS);
    }, DISMISS_MS);
}
