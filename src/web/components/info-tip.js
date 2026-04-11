/*
 * <info-tip> — Accessible "ⓘ" hint component.
 *
 * Phase 4. A small circled "i" that reveals a floating tooltip on
 * hover/focus. Used next to settings fields and non-obvious metrics
 * to give the user inline context without cluttering the layout.
 *
 * Usage:
 *   <info-tip text="Power is averaged over the last 4 seconds."></info-tip>
 *
 * Accessibility:
 *   - The control is keyboard-focusable (tabindex="0")
 *   - aria-describedby links it to the tooltip body
 *   - On focus the tooltip is visible; on blur it hides
 *   - Plain title attribute as a fallback for assistive tech that
 *     doesn't follow the aria-describedby relationship
 *
 * Implementation uses Lit from the CDN — no build step, no bundler.
 */

import { LitElement, html, css } from 'https://cdn.jsdelivr.net/npm/lit@3/index.js+esm';

class InfoTip extends LitElement {
    static properties = {
        text: { type: String },
        _open: { state: true },
    };

    constructor() {
        super();
        this.text = '';
        this._open = false;
    }

    static styles = css`
        :host {
            display: inline-flex;
            position: relative;
            vertical-align: middle;
            margin-left: var(--space-2, 8px);
        }

        .trigger {
            /* Reset UA <button> styles so the inherited defaults
             * don't fight the circular-pill design. Phase 7 switched
             * the trigger from <span role="button"> to a real
             * <button type="button"> for keyboard semantics; that
             * change pulled in browser-default padding, fonts, and
             * focus rings that need to be nulled out. */
            padding: 0;
            margin: 0;
            -webkit-appearance: none;
            appearance: none;

            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 16px;
            height: 16px;
            border-radius: 50%;
            background: var(--bg-tertiary, #fafafa);
            color: var(--text-tertiary, #999);
            font-size: 11px;
            font-weight: 700;
            font-family: var(--font-system, sans-serif);
            cursor: help;
            border: 1px solid var(--border-subtle, rgba(0, 0, 0, 0.1));
            /* Shared motion token — collapses to 0ms under
             * prefers-reduced-motion via the @media block in tokens.css. */
            transition:
                background var(--motion-fast, 150ms linear),
                color      var(--motion-fast, 150ms linear);
        }

        .trigger:hover, .trigger:focus-visible {
            background: var(--accent, #007aff);
            color: var(--text-on-accent, white);
            outline: none;
        }

        .tooltip {
            position: absolute;
            bottom: calc(100% + 8px);
            left: 50%;
            transform: translateX(-50%);
            background: var(--bg-secondary, #fff);
            color: var(--text-primary, #000);
            border: 1px solid var(--border-regular, rgba(0, 0, 0, 0.15));
            border-radius: var(--radius-md, 10px);
            padding: 8px 12px;
            font-size: var(--font-size-sm, 13px);
            line-height: var(--line-height-normal, 1.4);
            white-space: normal;
            width: max-content;
            max-width: 280px;
            box-shadow: var(--shadow-md, 0 2px 8px rgba(0, 0, 0, 0.15));
            z-index: 1000;
            pointer-events: none;
            opacity: 0;
            transition: opacity var(--motion-fast, 150ms linear);
        }

        .tooltip.open {
            opacity: 1;
        }
    `;

    render() {
        // Phase 7 / task #30: the trigger is a real <button> element,
        // not a <span role="button">. Real buttons get keyboard
        // Space/Enter activation semantics for free from the platform
        // — the old span-with-role required custom @keydown handling
        // (which we never added), so pressing Space on the span did
        // nothing. <button type="button"> also participates correctly
        // in form reset, prevents implicit submit when the info-tip
        // lives inside a <form>, and integrates with browser dev-tool
        // accessibility inspectors out of the box.
        //
        // The click handler toggles _open so a mouse user without
        // hover (e.g. iPad Safari) can also see the tooltip. Previous
        // behavior relied on hover-only which is a dead-end on touch.
        const toggleOpen = () => { this._open = !this._open; };
        return html`
            <button
                type="button"
                class="trigger"
                aria-describedby="info-tip-body"
                aria-expanded=${this._open ? 'true' : 'false'}
                aria-label=${`Help: ${this.text}`}
                title=${this.text}
                @click=${toggleOpen}
                @mouseenter=${() => (this._open = true)}
                @mouseleave=${() => (this._open = false)}
                @focus=${() => (this._open = true)}
                @blur=${() => (this._open = false)}
                @keydown=${(e) => {
                    if (e.key === 'Escape') this._open = false;
                }}
            >ⓘ</button>
            <span
                id="info-tip-body"
                class="tooltip ${this._open ? 'open' : ''}"
                role="tooltip"
            >${this.text}</span>
        `;
    }
}

customElements.define('info-tip', InfoTip);
