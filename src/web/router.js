/*
 * router.js — Hash-based router for the four sidebar views.
 *
 * Phase 4. Routes:
 *   #/dashboard  → Dashboard view (default)
 *   #/report     → Report view (placeholder until Phase 6)
 *   #/power      → Power view (placeholder until Phase 5)
 *   #/settings   → Settings view (placeholder until Phase 6)
 *
 * Hash-based rather than history API because the static file server
 * doesn't have per-path HTML rewrite rules — hash routing works at any
 * depth without server-side configuration. Phase 3's /api/* routes are
 * unaffected because the router only touches location.hash.
 *
 * Each registered view is a simple object:
 *   { name, mount(container, ctx), unmount() }
 *
 * The router calls mount() when entering a route and unmount() when
 * leaving, giving views a chance to tear down intervals / event
 * listeners cleanly.
 *
 * All DOM mutations in this file use textContent / createElement /
 * replaceChildren rather than innerHTML, so untrusted content (like
 * error messages from caught exceptions) can never be injected as
 * HTML. Individual views are free to build richer DOM but they must
 * follow the same discipline.
 */

const routes = new Map();
let currentRoute = null;
let mountContainer = null;
let routerContext = {};

function parseHash() {
    const hash = window.location.hash || '#/dashboard';
    // Strip the leading '#' and any query string
    return hash.replace(/^#/, '').split('?')[0] || '/dashboard';
}

function normalizeRoute(path) {
    // Ensure a leading slash and strip trailing slashes
    if (!path.startsWith('/')) path = '/' + path;
    if (path.length > 1 && path.endsWith('/')) path = path.slice(0, -1);
    return path;
}

function renderMountError(viewName, errMessage) {
    // Build the error state via safe DOM methods rather than innerHTML —
    // errMessage comes from a caught exception and may contain characters
    // that would be interpreted as HTML.
    const wrap = document.createElement('div');
    wrap.className = 'empty-state';

    const icon = document.createElement('div');
    icon.className = 'icon';
    icon.textContent = '⚠️';

    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = 'Failed to load view';

    const desc = document.createElement('div');
    desc.className = 'description';
    desc.textContent = `${viewName}: ${errMessage}`;

    wrap.append(icon, title, desc);
    mountContainer.replaceChildren(wrap);
}

async function activate(path) {
    const normalized = normalizeRoute(path);
    const view = routes.get(normalized) || routes.get('/dashboard');

    if (!view) {
        console.error(`router: no view registered for ${normalized} and no /dashboard fallback`);
        return;
    }

    // Tear down the previous view if any
    if (currentRoute && currentRoute !== view && typeof currentRoute.unmount === 'function') {
        try {
            currentRoute.unmount();
        } catch (err) {
            console.error(`router: unmount of ${currentRoute.name} threw:`, err);
        }
    }

    // Clear the mount container and let the new view render.
    // replaceChildren() with no arguments is the modern idiom for
    // "remove all children" — safer than innerHTML = ''.
    if (mountContainer) {
        mountContainer.replaceChildren();
    }

    currentRoute = view;
    try {
        await view.mount(mountContainer, routerContext);
    } catch (err) {
        console.error(`router: mount of ${view.name} threw:`, err);
        if (mountContainer) {
            renderMountError(view.name, err && err.message ? err.message : String(err));
        }
    }

    // Update sidebar nav highlighting
    document.querySelectorAll('aside.sidebar nav a').forEach(link => {
        const linkPath = normalizeRoute((link.getAttribute('href') || '').replace(/^#/, ''));
        if (linkPath === normalized) {
            link.setAttribute('aria-current', 'page');
        } else {
            link.removeAttribute('aria-current');
        }
    });
}

/* ─── Public API ────────────────────────────────────────────────────────── */

export function registerRoute(path, view) {
    const normalized = normalizeRoute(path);
    routes.set(normalized, { name: path, ...view });
}

export function initRouter(container, context = {}) {
    mountContainer = container;
    routerContext = context;

    window.addEventListener('hashchange', () => {
        activate(parseHash());
    });

    // Initial activation
    activate(parseHash());
}

export function navigate(path) {
    window.location.hash = '#' + normalizeRoute(path);
}

export function getCurrentRoute() {
    return currentRoute ? currentRoute.name : null;
}
