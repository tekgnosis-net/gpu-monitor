/*
 * views/report.js — Report view (placeholder for Phase 4).
 *
 * Phase 4 lays the sidebar navigation + routing infrastructure, but
 * the actual report content ships in Phase 6 alongside the email
 * scheduler + Jinja templates. For now this view just tells the user
 * the feature is coming, links back to the dashboard, and fails
 * gracefully if the user navigates here before the feature exists.
 */

export const reportView = {
    name: 'report',

    async mount(container) {
        const header = document.createElement('header');
        const h1 = document.createElement('h1');
        h1.textContent = 'Report';
        const subtitle = document.createElement('div');
        subtitle.className = 'subtitle';
        subtitle.textContent = 'HTML email reports on a schedule';
        header.append(h1, subtitle);
        container.append(header);

        const empty = document.createElement('div');
        empty.className = 'empty-state';

        const icon = document.createElement('div');
        icon.className = 'icon';
        icon.textContent = '📄';

        const title = document.createElement('div');
        title.className = 'title';
        title.textContent = 'Coming in Phase 6';

        const desc = document.createElement('div');
        desc.className = 'description';
        desc.textContent = 'Scheduled rich-HTML email reports with embedded PNG charts will be added here alongside the SMTP configuration UI. For now the Dashboard shows live telemetry.';

        empty.append(icon, title, desc);
        container.append(empty);
    },

    unmount() {
        /* no cleanup needed for a static view */
    },
};
