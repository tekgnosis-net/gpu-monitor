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
        return html`
            <span
                class="trigger"
                role="button"
                tabindex="0"
                aria-describedby="info-tip-body"
                title=${this.text}
                @mouseenter=${() => (this._open = true)}
                @mouseleave=${() => (this._open = false)}
                @focus=${() => (this._open = true)}
                @blur=${() => (this._open = false)}
                @keydown=${(e) => {
                    if (e.key === 'Escape') this._open = false;
                }}
            >ⓘ</span>
            <span
                id="info-tip-body"
                class="tooltip ${this._open ? 'open' : ''}"
                role="tooltip"
            >${this.text}</span>
        `;
    }
}

customElements.define('info-tip', InfoTip);
