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
 * becomes an overlay drawer. The data-sidebar-collapsed attribute is
 * still set from the persisted preference so it re-applies when the
 * user resizes back to desktop, but its *visual* effect is cancelled
 * by the mobile @media overrides in layout.css (drawer is always
 * full width with labels visible). Drawer open/closed state uses
 * data-sidebar-open instead.
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
    const next = !open;
    root.setAttribute('data-sidebar-open', next ? 'true' : 'false');
    // Reflect drawer state on the hamburger so AT users hear the
    // correct expanded/collapsed announcement AND the correct verb in
    // the accessible name. The button lives in <body>, not inside
    // <aside>, so we can't rely on a sibling selector — set the
    // attributes directly.
    const btn = document.querySelector('.mobile-menu-button');
    if (btn) {
        btn.setAttribute('aria-expanded', next ? 'true' : 'false');
        btn.setAttribute('aria-label', next ? 'Close navigation' : 'Open navigation');
    }
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
        // Always set an aria-label so the link has an accessible name
        // regardless of whether the sidebar is expanded (label span
        // visible) or collapsed (label span display:none, icon
        // aria-hidden). Without this, screen readers encounter an
        // unnamed link in the collapsed state.
        link.setAttribute('aria-label', item.label);

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
        // open over it. Also flip the hamburger's aria-expanded so
        // assistive tech hears "collapsed" after the navigation.
        link.addEventListener('click', () => {
            if (window.matchMedia('(max-width: 768px)').matches) {
                document.documentElement.setAttribute('data-sidebar-open', 'false');
                const hamburger = document.querySelector('.mobile-menu-button');
                if (hamburger) {
                    hamburger.setAttribute('aria-expanded', 'false');
                    hamburger.setAttribute('aria-label', 'Open navigation');
                }
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

function _createGitHubIcon() {
    // Build the GitHub octocat SVG via DOM methods (not innerHTML)
    // to satisfy the security hook's XSS policy. The path data is
    // the official GitHub mark from github.com/logos.
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('width', '14');
    svg.setAttribute('height', '14');
    svg.setAttribute('fill', 'currentColor');
    svg.style.verticalAlign = '-2px';
    svg.style.marginRight = '4px';
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d',
        'M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38' +
        ' 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13' +
        '-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66' +
        '.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15' +
        '-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0' +
        ' 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56' +
        '.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07' +
        '-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z');
    svg.append(path);
    return svg;
}

function buildFooter(version) {
    const footer = document.createElement('div');
    footer.className = 'sidebar-footer';

    const ver = document.createElement('div');
    ver.className = 'version';
    ver.textContent = `v${version}`;

    // GitHub link with octocat icon + star CTA
    const ghLink = document.createElement('a');
    ghLink.className = 'github-link';
    ghLink.href = 'https://github.com/tekgnosis-net/gpu-monitor';
    ghLink.target = '_blank';
    ghLink.rel = 'noopener noreferrer';
    ghLink.title = 'Star gpu-monitor on GitHub';
    ghLink.append(_createGitHubIcon());
    ghLink.append(document.createTextNode(' 🌟 Star us on GitHub'));

    const byline = document.createElement('span');
    byline.className = 'byline';
    byline.textContent = 'Built with love ❤️ by Tekgnosis';

    footer.append(ver, ghLink, byline);
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
    // aria-expanded pairs with aria-controls to tell AT that this
    // button opens/closes the sidebar drawer. Initial state is
    // "collapsed" — toggleMobileDrawer() flips it as the drawer opens.
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-controls', 'main-sidebar');
    btn.textContent = '☰';
    btn.addEventListener('click', toggleMobileDrawer);
    (target || document.body).append(btn);
}
