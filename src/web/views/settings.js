/*
 * views/settings.js — Settings view (placeholder for Phase 4).
 *
 * Phase 6 builds the full multi-tab settings UI (Collection / SMTP /
 * Alerts / Power / Housekeeping / Logging / Reports / Theme). Phase 4
 * ships just the empty state so the sidebar entry works.
 *
 * NOTE: Phase 4 DOES ship the theme toggle — it lives in the sidebar
 * itself rather than this view, because a theme toggle that's only
 * reachable by navigating somewhere feels wrong. The full theme
 * section in Settings (Phase 6) will duplicate the control with
 * more context.
 */

export const settingsView = {
    name: 'settings',

    async mount(container) {
        const header = document.createElement('header');
        const h1 = document.createElement('h1');
        h1.textContent = 'Settings';
        const subtitle = document.createElement('div');
        subtitle.className = 'subtitle';
        subtitle.textContent = 'Collection cadence, alerts, SMTP, housekeeping';
        header.append(h1, subtitle);
        container.append(header);

        const empty = document.createElement('div');
        empty.className = 'empty-state';

        const icon = document.createElement('div');
        icon.className = 'icon';
        icon.textContent = '⚙️';

        const title = document.createElement('div');
        title.className = 'title';
        title.textContent = 'Coming in Phase 6';

        const desc = document.createElement('div');
        desc.className = 'description';
        desc.textContent = 'This view will host configuration for poll cadence, alert thresholds, SMTP, database housekeeping, email report schedules, and the electricity rate used for kWh cost calculations. The theme toggle is already available in the sidebar.';

        empty.append(icon, title, desc);
        container.append(empty);
    },

    unmount() {
        /* no cleanup */
    },
};
