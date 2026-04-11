/*
 * sidebar.js — Collapsible sidebar with nav + version footer.
 *
 * Phase 4. Exposes mountSidebar() which the app entrypoint calls once
 * to build the sidebar DOM inside <aside class="sidebar">. The sidebar
 * itself is declared in gpu-stats.html as an empty <aside> tag; all
 * its content is generated here so the navigation items live in one
 * place instead of being maintained in the HTML and the router
 * independently.
 *
 * Collapse state persists in localStorage under
 * 'gpu-monitor:sidebar-collapsed'. On mobile (< 768px) the sidebar
 * becomes an overlay drawer and the collapse state is ignored in
 * favour of data-sidebar-open.
 *
 * Footer renders the version from /api/version and the "Built with
 * love ❤️ by Tekgnosis" byline.
 *
 * All DOM mutations use safe methods (createElement + textContent +
 * append) — no innerHTML with dynamic content.
 */

import { cycleTheme, getTheme } from './theme.js';

const STORAGE_KEY_COLLAPSED = 'gpu-monitor:sidebar-collapsed';

const NAV_ITEMS = [
    { path: '/dashboard', icon: '📊', label: 'Dashboard' },
    { path: '/report',    icon: '📄', label: 'Report' },
    { path: '/power',     icon: '⚡', label: 'Power usage' },
    { path: '/settings',  icon: '⚙️', label: 'Settings' },
];

function loadCollapsed() {
    try {
        return localStorage.getItem(STORAGE_KEY_COLLAPSED) === 'true';
    } catch {
        return false;
    }
}

function saveCollapsed(collapsed) {
    try {
        localStorage.setItem(STORAGE_KEY_COLLAPSED, collapsed ? 'true' : 'false');
    } catch {
        /* ignore */
    }
}

function applyCollapsed(collapsed) {
    document.documentElement.setAttribute(
        'data-sidebar-collapsed',
        collapsed ? 'true' : 'false'
    );
}

export function toggleSidebar() {
    const current = document.documentElement.getAttribute('data-sidebar-collapsed') === 'true';
    const next = !current;
    saveCollapsed(next);
    applyCollapsed(next);
}

export function toggleMobileDrawer() {
    const root = document.documentElement;
    const open = root.getAttribute('data-sidebar-open') === 'true';
    root.setAttribute('data-sidebar-open', open ? 'false' : 'true');
}

function buildHeader() {
    const header = document.createElement('div');
    header.className = 'sidebar-header';

    const logo = document.createElement('div');
    logo.className = 'logo';
    logo.textContent = 'GM';

    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = 'GPU Monitor';

    header.append(logo, title);
    return header;
}

function buildNav() {
    const nav = document.createElement('nav');
    for (const item of NAV_ITEMS) {
        const link = document.createElement('a');
        link.href = `#${item.path}`;

        const icon = document.createElement('span');
        icon.className = 'icon';
        icon.textContent = item.icon;
        icon.setAttribute('aria-hidden', 'true');

        const label = document.createElement('span');
        label.className = 'label';
        label.textContent = item.label;

        link.append(icon, label);

        // Close the mobile drawer when the user picks a nav item —
        // otherwise they'd land on the new view with the drawer still
        // open over it.
        link.addEventListener('click', () => {
            if (window.matchMedia('(max-width: 768px)').matches) {
                document.documentElement.setAttribute('data-sidebar-open', 'false');
            }
        });

        nav.append(link);
    }
    return nav;
}

function buildToggle() {
    const btn = document.createElement('button');
    btn.className = 'sidebar-toggle';
    btn.setAttribute('aria-label', 'Toggle sidebar');
    btn.setAttribute('title', 'Collapse / expand sidebar');
    btn.textContent = '◀';
    btn.addEventListener('click', toggleSidebar);
    return btn;
}

function buildThemeToggle() {
    const btn = document.createElement('button');
    btn.className = 'sidebar-toggle';
    btn.setAttribute('aria-label', 'Cycle theme');

    const updateLabel = () => {
        const mode = getTheme();
        const icons = { auto: '🌓', light: '☀️', dark: '🌙' };
        btn.textContent = icons[mode] || '🌓';
        btn.setAttribute('title', `Theme: ${mode} (click to cycle)`);
    };
    updateLabel();

    btn.addEventListener('click', () => {
        cycleTheme();
        updateLabel();
    });
    window.addEventListener('themechange', updateLabel);
    return btn;
}

function buildFooter(version) {
    const footer = document.createElement('div');
    footer.className = 'sidebar-footer';

    const ver = document.createElement('div');
    ver.className = 'version';
    ver.textContent = `v${version}`;

    const byline = document.createElement('span');
    byline.className = 'byline';
    byline.textContent = 'Built with love ❤️ by Tekgnosis';

    footer.append(ver, byline);
    return footer;
}

export function mountSidebar(container, { version }) {
    // Apply the persisted collapse state on first mount
    applyCollapsed(loadCollapsed());

    container.replaceChildren(
        buildHeader(),
        buildNav(),
        buildToggle(),
        buildThemeToggle(),
        buildFooter(version || 'unknown'),
    );
}

export function mountMobileMenuButton(target) {
    // Adds a hamburger button to <body> that toggles the mobile drawer.
    // The button is hidden via CSS above the 768px breakpoint.
    const existing = document.querySelector('.mobile-menu-button');
    if (existing) return;

    const btn = document.createElement('button');
    btn.className = 'mobile-menu-button';
    btn.setAttribute('aria-label', 'Open navigation');
    btn.textContent = '☰';
    btn.addEventListener('click', toggleMobileDrawer);
    (target || document.body).append(btn);
}
