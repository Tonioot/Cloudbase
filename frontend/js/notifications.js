// Notifications: the bell in the top bar and the /notifications inbox page.

import { api } from './api.js';
import { timeAgo } from './utils.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const BELL = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`;

const SEVERITY_LABEL = { critical: 'Critical', warning: 'Warning', info: 'Info', resolved: 'Resolved' };
const POLL_MS = 30000;

/** One inbox row. `unread` marks it with a dot. */
export function notificationHTML(n, { unread = false, full = false } = {}) {
  const state = n.state === 'open' ? '<span class="notif-state">Open</span>' : '';
  const muted = n.muted ? '<span class="notif-state notif-state--muted" title="Not sent: the app was muted">Muted</span>' : '';
  const meta = [n.app_name ? esc(n.app_name) : '', timeAgo(n.created_at)].filter(Boolean).join(' · ');
  const message = n.message ? `<div class="notif-msg${full ? '' : ' notif-msg--clamp'}">${esc(n.message)}</div>` : '';
  const tag = n.link ? 'a' : 'div';
  return `
    <${tag} class="notif${unread ? ' notif--unread' : ''}" data-sev="${esc(n.severity)}" ${n.link ? `href="${esc(n.link)}"` : ''}>
      <span class="notif-sev" title="${SEVERITY_LABEL[n.severity] || ''}"></span>
      <span class="notif-body">
        <span class="notif-title">${esc(n.title)}${state}${muted}</span>
        ${message}
        <span class="notif-meta">${meta}</span>
      </span>
    </${tag}>`;
}

const isUnread = (n, readAt) => !readAt || new Date(n.created_at) > new Date(readAt);

/* ─── Bell ──────────────────────────────────────────────────────────────── */

let pop = null;
let bellBtn = null;

function setBadge(summary) {
  if (!bellBtn) return;
  const badge = bellBtn.querySelector('.bell-badge');
  const n = summary?.unread || 0;
  badge.textContent = n > 99 ? '99+' : String(n);
  badge.hidden = !n;
  bellBtn.dataset.open = summary?.open ? '1' : '';
  bellBtn.setAttribute('aria-label', n ? `Notifications, ${n} unread` : 'Notifications');
}

async function refreshBadge() {
  try { setBadge(await api.getNotificationSummary()); } catch { /* no permission or offline */ }
}

function closePop() {
  pop?.classList.remove('open');
  bellBtn?.setAttribute('aria-expanded', 'false');
}

async function openPop() {
  if (!pop) {
    pop = document.createElement('div');
    pop.className = 'notif-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Notifications');
    document.body.appendChild(pop);
    pop.addEventListener('click', e => { if (e.target.closest('a')) closePop(); });
  }
  const rect = bellBtn.getBoundingClientRect();
  pop.style.top = `${rect.bottom + 8}px`;
  pop.style.right = `${Math.max(8, window.innerWidth - rect.right)}px`;
  pop.innerHTML = '<div class="notif-pop-head"><span>Notifications</span></div><div class="flyout-empty">Loading…</div>';
  pop.classList.add('open');
  bellBtn.setAttribute('aria-expanded', 'true');

  let data;
  try { data = await api.listNotifications({ limit: 8 }); }
  catch (e) { pop.querySelector('.flyout-empty').textContent = e.message; return; }
  const canManage = !(window._hiddenPerms?.has('notifications.manage'));
  pop.innerHTML = `
    <div class="notif-pop-head">
      <span>Notifications${data.open ? ` <span class="notif-open-count">${data.open} open</span>` : ''}</span>
      ${canManage ? '<a href="/settings?s=notifications" class="notif-pop-link">Settings</a>' : ''}
    </div>
    <div class="notif-pop-list">
      ${data.items.length
        ? data.items.map(n => notificationHTML(n, { unread: isUnread(n, data.read_at) })).join('')
        : '<div class="notif-empty">Nothing yet. Problems with deploys, instances, nodes, databases and certificates show up here.</div>'}
    </div>
    <a class="notif-pop-all" href="/notifications">View all</a>`;
  if (data.unread) {
    api.markNotificationsRead().then(() => setBadge({ unread: 0, open: data.open })).catch(() => {});
  }
}

/** Put the bell in the top bar (called by the shell on every page). */
export function mountBell(container) {
  if (!container || document.getElementById('btn-notifications')) return;
  bellBtn = document.createElement('button');
  bellBtn.type = 'button';
  bellBtn.className = 'bell-btn';
  bellBtn.id = 'btn-notifications';
  bellBtn.dataset.perm = 'notifications.view';
  bellBtn.setAttribute('aria-haspopup', 'dialog');
  bellBtn.setAttribute('aria-expanded', 'false');
  bellBtn.innerHTML = `${BELL}<span class="bell-badge" hidden></span>`;
  container.appendChild(bellBtn);

  bellBtn.addEventListener('click', e => {
    e.stopPropagation();
    if (pop?.classList.contains('open')) closePop(); else openPop();
  });
  document.addEventListener('click', e => { if (pop?.classList.contains('open') && !e.target.closest('.notif-pop')) closePop(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closePop(); });
  window.addEventListener('resize', closePop);

  refreshBadge();
  setInterval(() => { if (!document.hidden) refreshBadge(); }, POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshBadge(); });
}

/* ─── Inbox page ────────────────────────────────────────────────────────── */

export function initNotificationsPage() {
  const list = document.getElementById('notif-list');
  const more = document.getElementById('notif-more');
  const filter = document.getElementById('notif-filter');
  let readAt = null;
  let lastId = null;

  const load = async (append = false) => {
    if (!append) { list.innerHTML = '<div class="apps-empty">Loading…</div>'; lastId = null; }
    more.disabled = true;
    let data;
    try {
      data = await api.listNotifications({ limit: 50, before: lastId, state: filter.value });
    } catch (e) {
      list.innerHTML = `<div class="apps-empty">${esc(e.message)}</div>`;
      return;
    }
    if (!append) {
      readAt = data.read_at;
      document.getElementById('notif-summary').textContent = data.open
        ? `${data.open} open problem${data.open === 1 ? '' : 's'}` : 'No open problems';
    }
    const html = data.items.map(n => notificationHTML(n, { unread: isUnread(n, readAt), full: true })).join('');
    if (append) list.insertAdjacentHTML('beforeend', html);
    else list.innerHTML = html || `<div class="empty-state"><div class="empty-title">No notifications</div><p class="empty-sub">Deploys, crashed instances, offline nodes, failing backups and certificates that don't renew show up here.</p></div>`;
    lastId = data.items.length ? data.items[data.items.length - 1].id : lastId;
    more.style.display = data.items.length < 50 ? 'none' : '';
    more.disabled = false;
    if (!append && data.unread) {
      api.markNotificationsRead().then(() => setBadge({ unread: 0, open: data.open })).catch(() => {});
    }
  };

  filter.addEventListener('change', () => load());
  more.addEventListener('click', () => load(true));
  load();
}
