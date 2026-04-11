/*
 * views/power.js — Power usage view (placeholder for Phase 4).
 *
 * Phase 5 fills this view with per-GPU power graphs + kWh cost
 * approximation from an electricity rate stored in settings. Phase 4
 * only ships the sidebar entry and an empty state so navigation works.
 */

export const powerView = {
    name: 'power',

    async mount(container) {
        const header = document.createElement('header');
        const h1 = document.createElement('h1');
        h1.textContent = 'Power usage';
        const subtitle = document.createElement('div');
        subtitle.className = 'subtitle';
        subtitle.textContent = 'Per-GPU power draw, energy, and electricity cost';
        header.append(h1, subtitle);
        container.append(header);

        const empty = document.createElement('div');
        empty.className = 'empty-state';

        const icon = document.createElement('div');
        icon.className = 'icon';
        icon.textContent = '⚡';

        const title = document.createElement('div');
        title.className = 'title';
        title.textContent = 'Coming in Phase 5';

        const desc = document.createElement('div');
        desc.className = 'description';
        desc.textContent = 'This view will show per-GPU power trend charts, integrated energy (Wh/kWh), and a kWh-cost estimate based on an electricity rate you configure in Settings.';

        empty.append(icon, title, desc);
        container.append(empty);
    },

    unmount() {
        /* no cleanup */
    },
};
