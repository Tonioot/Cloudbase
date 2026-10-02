// App shell: icon rail, top bar and ⌘K command palette.
//
// Rendered as a side effect of importing this module (sidebar.js imports it),
// so the elements below exist before page scripts wire them up by id:
//   btn-pdm-nginx, btn-system-settings, btn-github-tokens, btn-export-import,
//   btn-manage-users, btn-change-password, btn-logout, session-timer-bar.

import { api } from './api.js';

const svg = (body, size = 17) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

export const ICONS = {
  logo:     `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 18a4.5 4.5 0 0 1-.6-8.96A6 6 0 0 1 18 8.5a4 4 0 0 1-.5 9.5z"/></svg>`,
  overview: svg('<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>'),
  apps:     svg('<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>'),
  nodes:    svg('<rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><line x1="7" y1="7.5" x2="7.01" y2="7.5"/><line x1="7" y1="16.5" x2="7.01" y2="16.5"/>'),
  audit:    svg('<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>'),
  database: svg('<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.66 3.58 3 8 3s8-1.34 8-3V5"/><path d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/>'),
  settings: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
  moon:     svg('<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>', 16),
  sun:      svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>', 16),
  monitor:  svg('<rect x="3" y="4" width="18" height="12" rx="2"/><line x1="8" y1="20" x2="16" y2="20"/><line x1="12" y1="16" x2="12" y2="20"/>', 16),
  search:   svg('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>', 14),
  globe:    svg('<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>', 15),
  sliders:  svg('<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>', 15),
  key:      svg('<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.78 7.78 5.5 5.5 0 0 1 7.78-7.78zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>', 15),
  transfer: svg('<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>', 15),
  users:    svg('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c0-3.6 2.9-6 6.5-6s6.5 2.4 6.5 6"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7"/><path d="M21.5 20c0-2.8-1.7-4.9-4-5.7"/>', 15),
  lock:     svg('<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>', 15),
  logout:   svg('<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>', 15),
  plus:     svg('<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>', 15),
  arrow:    svg('<polyline points="9 6 15 12 9 18"/>', 14),
};

const STATUS_COLOR = {
  running: 'var(--green)', online: 'var(--green)',
  stopped: 'var(--faint)',
  error: 'var(--red)', crashed: 'var(--red)', offline: 'var(--red)',
  deploying: 'var(--yellow)', starting: 'var(--yellow)', stopping: 'var(--yellow)', restarting: 'var(--yellow)',
};
export const statusColor = s => STATUS_COLOR[s] || 'var(--faint)';

export const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function currentSection() {
  const p = location.pathname;
  if (p.startsWith('/app')) return 'apps';
  if (p.startsWith('/node')) return 'nodes';
  if (p.startsWith('/audit')) return 'audit';
  return 'overview';
}

const DEFAULT_CRUMBS = {
  overview: ['Overview'],
  apps: ['Apps'],
  nodes: ['Nodes'],
  audit: ['Audit log'],
};

/* ─── Sidebar ───────────────────────────────────────────────────────────── */

function railHTML(active) {
  const nav = (key, href, label, extra = '') =>
    `<a class="sb-item${active === key ? ' active' : ''}" href="${href}" ${extra}>${ICONS[key]}<span>${label}</span></a>`;
  const setting = (id, icon, label, extra = '') =>
    `<button type="button" class="sb-item" id="${id}" ${extra}>${ICONS[icon]}<span>${label}</span></button>`;
  return `
    <div class="sb-head">
      <a href="/" class="rail-logo" aria-label="Cloudbase home">${ICONS.logo}</a>
      <div class="sb-brand">
        <span class="sb-brand-name">Cloudbase</span>
        <span class="sb-brand-host">${esc(location.host)}</span>
      </div>
    </div>

    <nav class="sb-nav" aria-label="Main">
      ${nav('overview', '/', 'Overview')}
      ${nav('audit', '/audit', 'Audit log', 'data-perm="audit.view"')}
      <span class="sb-item sb-item--soon" aria-disabled="true">${ICONS.database}<span>Databases</span><span class="sb-tag">Soon</span></span>

      <div class="sb-section"><span>Apps</span><span class="sb-count" id="rail-apps-count"></span></div>
      <div class="sb-list" id="rail-apps-list"><div class="flyout-empty">Loading…</div></div>

      <div class="sb-section"><span>Nodes</span><span class="sb-count" id="rail-nodes-count"></span></div>
      <div class="sb-list" id="rail-nodes-list"><div class="flyout-empty">Loading…</div></div>

      <div class="sb-section"><span>Settings</span></div>
      ${setting('btn-pdm-nginx', 'globe', 'Domain &amp; SSL', 'data-perm="system.manage"')}
      ${setting('btn-system-settings', 'sliders', 'System settings', 'data-perm="system.manage"')}
      ${setting('btn-manage-users', 'users', 'Users &amp; roles', 'style="display:none"')}
      ${setting('btn-github-tokens', 'key', 'GitHub tokens', 'data-perm="tokens.manage"')}
      ${setting('btn-export-import', 'transfer', 'Export / import', 'data-perm="apps.configure"')}
    </nav>

    <div class="sb-foot">
      <button type="button" class="sb-user" data-flyout="account" aria-haspopup="menu" aria-label="Account">
        <span class="rail-avatar" id="rail-avatar-initials">··</span>
        <span class="sb-user-text">
          <span class="sb-user-name" id="sb-user-name">Signed in</span>
          <span class="sb-user-role" id="sb-user-role"></span>
        </span>
      </button>
      <button type="button" class="sb-icon-btn" id="btn-theme" aria-label="Theme">${ICONS.monitor}</button>
    </div>

    <div class="flyout flyout--bottom" data-flyout-panel="account" role="menu" aria-label="Account">
      <div class="flyout-user">
        <span class="flyout-user-name" id="rail-user-name">Signed in</span>
        <span class="flyout-user-role" id="rail-user-role"></span>
      </div>
      <div id="session-timer-bar" class="session-timer-bar">
        <div class="session-timer-label">Session: —</div>
        <div class="session-timer-track"><div class="session-timer-fill" style="width:100%"></div></div>
      </div>
      <button type="button" class="menu-item" id="btn-change-password">${ICONS.lock}<span>Change password</span></button>
      <button type="button" class="menu-item menu-item--danger" id="btn-logout">${ICONS.logout}<span>Sign out</span></button>
    </div>`;
}

let openPanel = null;

function closeFlyouts() {
  document.querySelectorAll('.flyout.open').forEach(f => f.classList.remove('open'));
  document.querySelectorAll('[data-flyout][aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
  openPanel = null;
}

function toggleFlyout(btn) {
  const name = btn.dataset.flyout;
  const panel = document.querySelector(`[data-flyout-panel="${name}"]`);
  if (!panel) return;
  if (openPanel === panel) { closeFlyouts(); return; }
  closeFlyouts();
  const rect = btn.getBoundingClientRect();
  if (panel.classList.contains('flyout--bottom')) {
    panel.style.top = '';
    panel.style.bottom = `${Math.max(8, window.innerHeight - rect.top + 6)}px`;
  } else {
    panel.style.bottom = '';
    panel.style.top = `${Math.max(8, Math.min(rect.top - 6, window.innerHeight - 360))}px`;
  }
  panel.classList.add('open');
  btn.setAttribute('aria-expanded', 'true');
  openPanel = panel;
}

function syncThemeButton() {
  const btn = document.getElementById('btn-theme');
  if (!btn || !window.cbTheme) return;
  const choice = window.cbTheme.get();
  btn.innerHTML = choice === 'light' ? ICONS.sun : choice === 'dark' ? ICONS.moon : ICONS.monitor;
  const label = `Theme: ${choice === 'system' ? 'system' : choice}`;
  btn.title = label;
  btn.setAttribute('aria-label', label);
}

/* ─── Top bar ───────────────────────────────────────────────────────────── */

function topbarHTML() {
  return `
    <button type="button" class="nav-toggle" id="btn-nav" aria-label="Open navigation" aria-controls="rail" aria-expanded="false">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="20" y2="17"/></svg>
    </button>
    <nav class="topbar-crumbs" id="topbar-crumbs" aria-label="Breadcrumb"></nav>
    <div class="topbar-actions" id="topbar-actions"></div>
    <button type="button" class="search-trigger" id="btn-search" aria-label="Search or run a command">
      ${ICONS.search}<span class="search-trigger-text">Search or run a command</span><kbd>${navigator.platform.includes('Mac') ? '⌘' : 'Ctrl'} K</kbd>
    </button>`;
}

/** Set the breadcrumb: items are strings or { label, href }. */
export function setCrumbs(items) {
  const el = document.getElementById('topbar-crumbs');
  if (!el) return;
  el.innerHTML = items.map((it, i) => {
    const last = i === items.length - 1;
    const label = esc(typeof it === 'string' ? it : it.label);
    const href = typeof it === 'object' && it.href;
    const node = href && !last ? `<a href="${esc(href)}">${label}</a>` : `<span${last ? ' aria-current="page"' : ''}>${label}</span>`;
    return (i ? '<span class="crumb-sep" aria-hidden="true">/</span>' : '') + node;
  }).join('');
}

/* ─── Shared index of apps & nodes (rail flyouts + palette) ─────────────── */

let index = { apps: [], nodes: [], loaded: false };

/** Called by sidebar.js whenever apps/nodes are (re)loaded. */
export function updateIndex(apps, nodes) {
  index = { apps: apps || [], nodes: nodes || [], loaded: true };
  const section = currentSection();
  const q = new URLSearchParams(location.search);
  const currentId = parseInt(q.get('id'), 10);

  const appsEl = document.getElementById('rail-apps-list');
  if (appsEl) {
    document.getElementById('rail-apps-count').textContent = index.apps.length || '';
    appsEl.innerHTML = index.apps.length
      ? index.apps.map(a => `
          <a class="menu-item${section === 'apps' && a.id === currentId ? ' active' : ''}" href="/app?id=${a.id}">
            <span class="dot" style="background:${statusColor(a.status)}"></span>
            <span class="menu-item-label">${esc(a.name)}</span>
            <span class="menu-item-meta">${esc(a.no_web ? 'worker' : a.app_type === 'static' ? 'static' : a.app_type || '')}</span>
          </a>`).join('')
      : '<div class="flyout-empty">No apps yet</div>';
  }

  const nodesEl = document.getElementById('rail-nodes-list');
  if (nodesEl) {
    document.getElementById('rail-nodes-count').textContent = index.nodes.length || '';
    nodesEl.innerHTML = index.nodes.length
      ? index.nodes.map(n => `
          <a class="menu-item${section === 'nodes' && n.id === currentId ? ' active' : ''}" href="/node?id=${n.id}">
            <span class="dot" style="background:${statusColor(n.status)}"></span>
            <span class="menu-item-label">${esc(n.is_local ? 'primary' : n.name)}</span>
            <span class="menu-item-meta">${esc(n.is_local ? 'local' : n.status || '')}</span>
          </a>`).join('')
      : '<div class="flyout-empty">No nodes</div>';
  }
}

/* ─── Command palette ───────────────────────────────────────────────────── */

let palette = null;
let paletteItems = [];
let paletteSel = 0;

function paletteCommands() {
  const go = href => () => { location.href = href; };
  const click = id => () => document.getElementById(id)?.click();
  const onDashboard = currentSection() === 'overview';
  const cmds = [
    { group: 'Actions', label: 'New app', icon: ICONS.plus, run: onDashboard && document.getElementById('btn-deploy') ? click('btn-deploy') : go('/?action=new-app'), keywords: 'deploy create application' },
    { group: 'Actions', label: 'Add node', icon: ICONS.nodes, run: onDashboard && document.getElementById('btn-add-node') ? click('btn-add-node') : go('/?action=add-node'), keywords: 'server invite' },
    { group: 'Actions', label: 'Toggle theme', icon: ICONS.moon, run: () => { window.cbTheme?.cycle(); syncThemeButton(); }, keywords: 'dark light appearance' },
    { group: 'Go to', label: 'Overview', icon: ICONS.overview, run: go('/'), keywords: 'dashboard home' },
    { group: 'Go to', label: 'Audit log', icon: ICONS.audit, run: go('/audit'), keywords: 'history events' },
  ];
  for (const a of index.apps) {
    cmds.push({ group: 'Apps', label: a.name, dot: statusColor(a.status), meta: a.status || '', run: go(`/app?id=${a.id}`), keywords: `${a.domain || ''} ${a.app_type || ''}` });
  }
  for (const n of index.nodes) {
    cmds.push({ group: 'Nodes', label: n.is_local ? 'primary' : n.name, dot: statusColor(n.status), meta: n.status || '', run: go(`/node?id=${n.id}`), keywords: n.public_host || '' });
  }
  const tail = [
    ['btn-pdm-nginx', 'Domain & SSL', ICONS.globe], ['btn-system-settings', 'System settings', ICONS.sliders],
    ['btn-manage-users', 'Users & roles', ICONS.users], ['btn-github-tokens', 'GitHub tokens', ICONS.key],
    ['btn-export-import', 'Export / import apps', ICONS.transfer], ['btn-change-password', 'Change password', ICONS.lock],
    ['btn-logout', 'Sign out', ICONS.logout],
  ];
  for (const [id, label, ic] of tail) {
    const el = document.getElementById(id);
    if (el && getComputedStyle(el).display !== 'none') cmds.push({ group: 'Settings', label, icon: ic, run: click(id) });
  }
  return cmds;
}

function renderPalette() {
  const q = palette.querySelector('input').value.trim().toLowerCase();
  const all = paletteCommands();
  paletteItems = q
    ? all.filter(c => `${c.label} ${c.keywords || ''} ${c.group}`.toLowerCase().includes(q))
    : all.filter(c => c.group !== 'Settings');
  paletteSel = Math.min(paletteSel, Math.max(0, paletteItems.length - 1));

  const list = palette.querySelector('.palette-list');
  if (!paletteItems.length) {
    list.innerHTML = '<div class="palette-empty">No results</div>';
    return;
  }
  let html = '';
  let lastGroup = null;
  paletteItems.forEach((c, i) => {
    if (c.group !== lastGroup) { html += `<div class="palette-group">${esc(c.group)}</div>`; lastGroup = c.group; }
    const lead = c.dot ? `<span class="dot" style="background:${c.dot}"></span>` : (c.icon || '');
    html += `<button type="button" class="palette-item${i === paletteSel ? ' selected' : ''}" data-i="${i}" role="option" aria-selected="${i === paletteSel}">
      <span class="palette-lead">${lead}</span><span class="palette-label">${esc(c.label)}</span>${c.meta ? `<span class="palette-meta">${esc(c.meta)}</span>` : ''}
    </button>`;
  });
  list.innerHTML = html;
  list.querySelector('.palette-item.selected')?.scrollIntoView({ block: 'nearest' });
}

function runPaletteItem(i) {
  const c = paletteItems[i];
  if (!c) return;
  closePalette();
  c.run();
}

export function openPalette() {
  if (!palette) {
    palette = document.createElement('div');
    palette.className = 'palette-backdrop';
    palette.innerHTML = `
      <div class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <div class="palette-input">${ICONS.search}<input type="text" placeholder="Search apps, nodes, or run a command…" aria-label="Search" autocomplete="off" spellcheck="false" /><kbd>Esc</kbd></div>
        <div class="palette-list" role="listbox"></div>
      </div>`;
    document.body.appendChild(palette);
    const input = palette.querySelector('input');
    input.addEventListener('input', () => { paletteSel = 0; renderPalette(); });
    input.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown') { e.preventDefault(); paletteSel = Math.min(paletteSel + 1, paletteItems.length - 1); renderPalette(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); paletteSel = Math.max(paletteSel - 1, 0); renderPalette(); }
      else if (e.key === 'Enter') { e.preventDefault(); runPaletteItem(paletteSel); }
      else if (e.key === 'Escape') { closePalette(); }
    });
    palette.addEventListener('click', e => {
      const item = e.target.closest('.palette-item');
      if (item) runPaletteItem(parseInt(item.dataset.i, 10));
      else if (e.target === palette) closePalette();
    });
  }
  palette.classList.add('open');
  const input = palette.querySelector('input');
  input.value = '';
  paletteSel = 0;
  renderPalette();
  input.focus();
  if (!index.loaded) {
    Promise.allSettled([api.listApps(), api.listNodes()]).then(([a, n]) => {
      updateIndex(a.status === 'fulfilled' ? a.value : [], n.status === 'fulfilled' ? n.value : []);
      if (palette.classList.contains('open')) renderPalette();
    });
  }
}

function closePalette() {
  palette?.classList.remove('open');
}

/* ─── Mount ─────────────────────────────────────────────────────────────── */

function mount() {
  const rail = document.getElementById('rail');
  if (!rail) return;
  const section = currentSection();
  rail.innerHTML = railHTML(section);

  const main = document.querySelector('.main-content');
  if (main && !document.getElementById('topbar')) {
    const bar = document.createElement('header');
    bar.className = 'topbar';
    bar.id = 'topbar';
    bar.innerHTML = topbarHTML();
    main.prepend(bar);
    setCrumbs(DEFAULT_CRUMBS[section]);
  }

  rail.addEventListener('click', e => {
    const trigger = e.target.closest('[data-flyout]');
    if (trigger) { e.stopPropagation(); toggleFlyout(trigger); return; }
    if (e.target.closest('.flyout .menu-item')) closeFlyouts();
  });
  document.addEventListener('click', e => { if (openPanel && !e.target.closest('.flyout')) closeFlyouts(); });

  // In-page dropdowns: <div class="menu-wrap"><button data-dropdown>…</button><div class="dropdown">…</div></div>
  document.addEventListener('click', e => {
    const trigger = e.target.closest('[data-dropdown]');
    const openMenus = document.querySelectorAll('.menu-wrap.open');
    if (trigger) {
      const wrap = trigger.closest('.menu-wrap');
      const wasOpen = wrap.classList.contains('open');
      openMenus.forEach(w => { w.classList.remove('open'); w.querySelector('[data-dropdown]')?.setAttribute('aria-expanded', 'false'); });
      if (!wasOpen) { wrap.classList.add('open'); trigger.setAttribute('aria-expanded', 'true'); }
      return;
    }
    // Clicking an item, or anywhere outside, closes open dropdowns
    if (!e.target.closest('.dropdown') || e.target.closest('.dropdown .menu-item')) {
      openMenus.forEach(w => { w.classList.remove('open'); w.querySelector('[data-dropdown]')?.setAttribute('aria-expanded', 'false'); });
    }
  });
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); }
    else if (e.key === 'Escape') {
      if (openPanel) closeFlyouts();
      document.querySelectorAll('.menu-wrap.open').forEach(w => w.classList.remove('open'));
    }
  });
  window.addEventListener('resize', closeFlyouts);
  document.getElementById('btn-search')?.addEventListener('click', openPalette);

  // Small screens: the sidebar slides in over the page
  const navBtn = document.getElementById('btn-nav');
  const setNav = open => {
    document.body.classList.toggle('nav-open', open);
    navBtn?.setAttribute('aria-expanded', String(open));
  };
  navBtn?.addEventListener('click', e => { e.stopPropagation(); setNav(!document.body.classList.contains('nav-open')); });
  document.addEventListener('click', e => {
    if (document.body.classList.contains('nav-open') && !e.target.closest('#rail')) setNav(false);
  });

  const themeBtn = document.getElementById('btn-theme');
  themeBtn?.addEventListener('click', () => { window.cbTheme?.cycle(); syncThemeButton(); });
  syncThemeButton();

  window.addEventListener('cloudbase-role-ready', e => {
    const name = e.detail?.username || 'admin';
    document.getElementById('rail-user-name').textContent = name;
    document.getElementById('sb-user-name').textContent = name;
    document.getElementById('rail-avatar-initials').textContent = name.slice(0, 2).toUpperCase();
    const role = e.detail?.role;
    if (role) {
      document.getElementById('rail-user-role').textContent = role;
      document.getElementById('sb-user-role').textContent = role;
    }
  });
}

mount();
