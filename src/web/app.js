/*
 * app.js — Entry point for the Phase 4 frontend.
 *
 * Wires the pieces together:
 *   1. Initialize theming so first paint is in the right colors
 *   2. Fetch the version for the sidebar footer
 *   3. Mount the sidebar + mobile-menu button
 *   4. Register the four views with the router
 *   5. Initialize the router (which activates the current hash route)
 *
 * Everything is top-level-awaited so any error surfaces immediately in
 * the console. Views are imported lazily? No — Phase 4 imports them
 * eagerly because the combined JS footprint is tiny (~15KB unminified)
 * and there's no build step to set up route-based code splitting. Lazy
 * imports can come in a later phase if the bundle grows.
 */

import { initTheme } from './theme.js';
import { initRouter, registerRoute } from './router.js';
import { mountSidebar, mountMobileMenuButton } from './sidebar.js';
import { installKeybindings } from './keybindings.js';
import * as api from './api.js';

import { dashboardView } from './views/dashboard.js';
import { reportView }    from './views/report.js';
import { powerView }     from './views/power.js';
import { settingsView }  from './views/settings.js';

async function main() {
    // Apply the user's theme choice BEFORE any DOM rendering so we
    // don't flash the wrong color palette on first paint.
    initTheme();

    // Fetch the app version for the sidebar footer. /api/version is
    // a cheap single-field response and failures fall back to
    // 'unknown' via the api.js wrapper.
    const version = await api.getVersion();

    // Mount the sidebar into its placeholder <aside class="sidebar">
    const sidebar = document.querySelector('aside.sidebar');
    if (sidebar) {
        mountSidebar(sidebar, { version });
    } else {
        console.error('app: no <aside class="sidebar"> element found');
    }

    // The mobile hamburger button is injected into <body> and only
    // visible below the 768px breakpoint (CSS-controlled).
    mountMobileMenuButton();

    // Register the four views
    registerRoute('/dashboard', dashboardView);
    registerRoute('/report',    reportView);
    registerRoute('/power',     powerView);
    registerRoute('/settings',  settingsView);

    // Initialize the router with the <main> container
    const mainContainer = document.querySelector('main.content');
    if (mainContainer) {
        initRouter(mainContainer);
    } else {
        console.error('app: no <main class="content"> element found');
    }

    // Phase 7: install global keyboard shortcuts (g-prefix nav,
    // t = theme, \\ = sidebar, ? = cheat sheet). Must be after
    // router init so navigate() resolves to registered views.
    installKeybindings();
}

main().catch((err) => {
    console.error('app: initialization failed:', err);
});
