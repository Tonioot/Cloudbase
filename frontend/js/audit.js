import { api, PermissionError } from './api.js';
import { auditTableHTML } from './utils.js';

const LIMIT = 100;
let page = 0;
let hasMore = true;
let allLoaded = [];

export async function initAuditLogs() {
  await loadPage();
  document.getElementById('btn-load-more')?.addEventListener('click', loadMore);
  document.getElementById('filter-actor')?.addEventListener('input', renderFiltered);
  document.getElementById('filter-action')?.addEventListener('change', renderFiltered);
}

async function loadPage() {
  const wrap = document.getElementById('audit-log-wrap');
  if (page === 0) wrap.innerHTML = '<div style="padding:20px;color:var(--text-muted);font-size:13px">Loading…</div>';
  try {
    const entries = await api.getAuditLog(null, LIMIT, page * LIMIT);
    hasMore = entries.length === LIMIT;
    allLoaded.push(...entries);
    syncActionFilterOptions();
    renderFiltered();
    const btn = document.getElementById('btn-load-more');
    if (btn) btn.style.display = hasMore ? '' : 'none';
    page++;
  } catch (e) {
    const msg = e instanceof PermissionError
      ? 'Je hebt geen toegang tot de audit logs (audit.view vereist).'
      : e.message;
    wrap.innerHTML = `<div style="padding:20px;color:var(--red);font-size:13px">${msg}</div>`;
  }
}

function syncActionFilterOptions() {
  const sel = document.getElementById('filter-action');
  if (!sel) return;

  const prev = sel.value || '';
  const actions = Array.from(new Set(allLoaded.map(e => e.action).filter(Boolean))).sort();

  sel.innerHTML = '<option value="">All actions</option>' +
    actions.map(a => `<option value="${a}">${a}</option>`).join('');

  if (prev && actions.includes(prev)) {
    sel.value = prev;
  }
}

async function loadMore() {
  const btn = document.getElementById('btn-load-more');
  if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
  await loadPage();
  if (btn) { btn.disabled = false; btn.textContent = 'Load more'; }
}

function renderFiltered() {
  const actor  = (document.getElementById('filter-actor')?.value  || '').toLowerCase();
  const action = document.getElementById('filter-action')?.value  || '';

  let entries = allLoaded;
  if (actor)  entries = entries.filter(e => (e.actor || '').toLowerCase().includes(actor));
  if (action) entries = entries.filter(e => e.action === action);

  const wrap = document.getElementById('audit-log-wrap');
  if (!entries.length) {
    wrap.innerHTML = '<div class="apps-empty">No events found.</div>';
    return;
  }

  wrap.innerHTML = auditTableHTML(entries, { showApp: true });
}
