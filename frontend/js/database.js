import { api } from './api.js';
import { badge, toast, confirm, fmtUptime } from './utils.js';
import { esc, setCrumbs } from './shell.js';

const DB_ID = parseInt(new URLSearchParams(location.search).get('id'), 10);
const TRANSITIONAL = ['creating', 'starting', 'stopping'];

let database = null;
let credentials = null;
let pollTimer = null;

export async function initDatabasePage() {
  if (!DB_ID) { location.href = '/'; return; }

  document.getElementById('btn-start').onclick = () => lifecycle('start');
  document.getElementById('btn-stop').onclick = () => lifecycle('stop');
  document.getElementById('btn-restart').onclick = () => lifecycle('restart');
  document.getElementById('btn-reveal').onclick = toggleCredentials;
  document.getElementById('btn-copy-url').onclick = copyUrl;
  document.getElementById('btn-link').onclick = openLinkDialog;
  document.getElementById('btn-save-limits').onclick = saveLimits;
  document.getElementById('btn-refresh-logs').onclick = loadLogs;
  document.getElementById('btn-delete').onclick = deleteDatabase;

  await load();
  loadStats();
  loadLogs();
  setInterval(loadStats, 10000);
}

async function load() {
  try {
    database = await api.getDatabase(DB_ID);
  } catch (e) {
    toast(e.message, 'error');
    setTimeout(() => { location.href = '/'; }, 1500);
    return;
  }
  render();
  clearTimeout(pollTimer);
  const busy = TRANSITIONAL.includes(database.status) || database.busy;
  pollTimer = setTimeout(load, busy ? 2500 : 10000);
}

function render() {
  const d = database;
  document.title = `Cloudbase — ${d.name}`;
  setCrumbs([{ label: 'Databases', href: '/#databases-section' }, d.name]);

  document.getElementById('db-name').textContent = d.name;
  document.getElementById('db-meta').textContent = `${d.engine_label} ${d.version} · on ${d.node_name || 'unknown node'}`;
  document.getElementById('db-status-badge').innerHTML = badge(d.status);
  document.getElementById('db-status-meta').textContent = d.busy && !TRANSITIONAL.includes(d.status) ? 'Working…' : '';

  const err = document.getElementById('db-error');
  const showErr = d.last_error && ['error', 'unhealthy'].includes(d.status);
  err.style.display = showErr ? '' : 'none';
  err.textContent = showErr ? d.last_error : '';

  const stopped = d.desired_state === 'stopped';
  const blocked = d.busy || TRANSITIONAL.includes(d.status) || !d.node_online;
  document.getElementById('btn-start').style.display = stopped ? '' : 'none';
  document.getElementById('btn-stop').style.display = stopped ? 'none' : '';
  ['btn-start', 'btn-stop', 'btn-restart', 'btn-save-limits'].forEach(id => {
    document.getElementById(id).disabled = blocked;
  });

  document.getElementById('c-host').textContent = d.host;
  document.getElementById('c-port').textContent = d.port;
  if (!credentials) {
    document.getElementById('c-database').textContent = '—';
    document.getElementById('c-user').textContent = '—';
  }

  const cpuIn = document.getElementById('lim-cpu');
  const memIn = document.getElementById('lim-mem');
  if (document.activeElement !== cpuIn) cpuIn.value = d.cpu_limit ?? '';
  if (document.activeElement !== memIn) memIn.value = d.memory_limit_mb ?? '';

  renderLinks();
  if (window._applyPermVisibility) window._applyPermVisibility(document.body);
}

function renderLinks() {
  const list = document.getElementById('links-list');
  const links = database.links || [];
  if (!links.length) {
    list.innerHTML = '<div class="db-links-empty">No apps linked yet.</div>';
    return;
  }
  list.innerHTML = links.map(l => `
    <div class="db-link-row">
      <div class="db-link-main">
        <a href="/app?id=${l.app_id}" class="db-link-app">${esc(l.app_name)}</a>
        <code class="db-link-env">${esc(l.env_var_name)}</code>
        ${l.instances_elsewhere ? `<div class="db-link-note">Instances on other nodes connect through the primary (a few ms extra per query).</div>` : ''}
      </div>
      <button class="btn btn-sm btn-ghost" data-unlink="${l.id}" data-perm="databases.manage">Unlink</button>
    </div>`).join('');
  list.querySelectorAll('[data-unlink]').forEach(btn => {
    btn.onclick = async () => {
      const link = links.find(l => l.id === parseInt(btn.dataset.unlink, 10));
      const ok = await confirm(`Unlink ${esc(link.app_name)}?`,
        `${esc(link.app_name)} no longer gets <code>${esc(link.env_var_name)}</code> after its next restart.`);
      if (!ok) return;
      try {
        await api.unlinkDatabase(DB_ID, link.id);
        toast(`Unlinked. Restart ${link.app_name} to apply.`);
        load();
      } catch (e) { toast(e.message, 'error'); }
    };
  });
}

async function lifecycle(action) {
  if (action === 'stop' || action === 'restart') {
    const ok = await confirm(`${action === 'stop' ? 'Stop' : 'Restart'} ${esc(database.name)}?`,
      'Linked apps lose their database connection until it is running again.');
    if (!ok) return;
  }
  const fns = { start: api.startDatabase, stop: api.stopDatabase, restart: api.restartDatabase };
  try {
    await fns[action](DB_ID);
    toast(`${database.name}: ${action} requested`);
    load();
  } catch (e) { toast(e.message, 'error'); }
}

async function toggleCredentials() {
  const btn = document.getElementById('btn-reveal');
  if (credentials) {
    credentials = null;
    document.getElementById('c-password').textContent = '••••••••••••';
    document.getElementById('c-url').textContent = '••••••••••••';
    btn.textContent = 'Show credentials';
    render();
    return;
  }
  try {
    credentials = await api.getDatabaseCredentials(DB_ID);
  } catch (e) { toast(e.message, 'error'); return; }
  document.getElementById('c-database').textContent = credentials.database;
  document.getElementById('c-user').textContent = credentials.user;
  document.getElementById('c-password').textContent = credentials.password;
  document.getElementById('c-url').textContent = credentials.url;
  btn.textContent = 'Hide credentials';
}

async function copyUrl() {
  try {
    const creds = credentials || await api.getDatabaseCredentials(DB_ID);
    await navigator.clipboard.writeText(creds.url);
    toast('Connection URL copied');
  } catch (e) { toast(e.message, 'error'); }
}

async function loadStats() {
  if (!database || database.status === 'node_offline') return;
  const set = (id, v) => { document.getElementById(id).textContent = v; };
  try {
    const s = await api.getDatabaseStats(DB_ID);
    if (!s || s.cpu_percent === undefined) { ['s-cpu', 's-mem', 's-disk', 's-uptime'].forEach(id => set(id, '—')); return; }
    const limit = database.memory_limit_mb ? ` / ${database.memory_limit_mb} MB` : '';
    set('s-cpu', `${s.cpu_percent.toFixed(1)}%`);
    set('s-mem', `${Math.round(s.memory_mb)} MB${limit}`);
    set('s-disk', s.disk_mb !== undefined ? (s.disk_mb >= 1024 ? `${(s.disk_mb / 1024).toFixed(2)} GB` : `${s.disk_mb} MB`) : '—');
    set('s-uptime', fmtUptime(s.uptime_seconds || 0));
  } catch {
    // Node busy or offline; keep the last values.
  }
}

async function loadLogs() {
  const pre = document.getElementById('db-logs');
  try {
    const { lines } = await api.getDatabaseLogs(DB_ID, 300);
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = lines.length ? lines.join('\n') : 'No log output yet.';
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch (e) {
    pre.textContent = `Could not load logs: ${e.message}`;
  }
}

async function saveLimits() {
  const cpu = parseFloat(document.getElementById('lim-cpu').value);
  const mem = parseInt(document.getElementById('lim-mem').value, 10);
  const body = {
    cpu_limit: Number.isFinite(cpu) ? cpu : null,
    memory_limit_mb: Number.isInteger(mem) ? mem : null,
    clear_cpu_limit: !Number.isFinite(cpu),
    clear_memory_limit: !Number.isInteger(mem),
  };
  if (database.desired_state === 'running') {
    const ok = await confirm('Apply new limits?', 'The database container is replaced, so the database restarts briefly. The data stays.');
    if (!ok) return;
  }
  try {
    const res = await api.updateDatabase(DB_ID, body);
    toast(res.changed ? 'Limits saved' : 'Nothing changed');
    load();
  } catch (e) { toast(e.message, 'error'); }
}

async function openLinkDialog() {
  let apps;
  try { apps = await api.listApps(); } catch (e) { toast(e.message, 'error'); return; }
  const linked = new Set((database.links || []).map(l => l.app_id));
  const candidates = apps.filter(a => !linked.has(a.id));
  if (!candidates.length) { toast('Every app is already linked to this database'); return; }

  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="link-dlg-title">
      <div class="dialog-title" id="link-dlg-title">Link an app</div>
      <div class="dialog-body">
        <p class="form-dialog-intro">The app gets the connection URL of <strong>${esc(database.name)}</strong> as an environment variable after its next restart.</p>
        <div class="field" style="margin:0">
          <label class="field-label" for="link-app">App</label>
          <select id="link-app" class="input">
            ${candidates.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('')}
          </select>
        </div>
        <div class="field" style="margin:0">
          <label class="field-label" for="link-env">Environment variable</label>
          <input id="link-env" class="input input-mono" value="${esc(database.default_env_var)}" spellcheck="false" autocomplete="off" />
          <div class="field-hint">Instances on <strong>${esc(database.node_name || 'the same node')}</strong> connect directly; instances on other nodes go through the primary.</div>
        </div>
        <div class="field-hint" id="link-error" style="color:var(--red);display:none;margin:0"></div>
      </div>
      <div class="dialog-actions">
        <button class="btn" id="link-cancel">Cancel</button>
        <button class="btn btn-primary" id="link-ok">Link app</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  backdrop.querySelector('#link-cancel').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  backdrop.querySelector('#link-ok').onclick = async () => {
    const appId = parseInt(backdrop.querySelector('#link-app').value, 10);
    const envVar = backdrop.querySelector('#link-env').value.trim().toUpperCase();
    const errEl = backdrop.querySelector('#link-error');
    try {
      await api.linkDatabase(DB_ID, appId, envVar);
      close();
      const app = candidates.find(a => a.id === appId);
      toast(`Linked. Restart ${app ? app.name : 'the app'} to apply.`);
      load();
    } catch (e) {
      errEl.textContent = e.message;
      errEl.style.display = '';
    }
  };
}

async function deleteDatabase() {
  const d = database;
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="del-dlg-title">
      <div class="dialog-title" id="del-dlg-title">Delete ${esc(d.name)}</div>
      <div class="dialog-body">
        <p class="form-dialog-intro">This removes the database container${d.link_count ? ` and the links to ${d.link_count} app${d.link_count === 1 ? '' : 's'}` : ''}.${d.node_online ? '' : ` <strong>Node ${esc(d.node_name || '')} is offline</strong>: only the entry in Cloudbase is removed; the container and its data stay on the node.`}</p>
        ${d.node_online ? `
        <label class="toggle-row form-toggle">
          <span><span class="field-label">Keep the data volume</span><span class="field-hint">Leaves <code>cloudbase-db-${d.id}-data</code> on the node</span></span>
          <span class="toggle"><input type="checkbox" id="del-keep" /><span class="toggle-slider"></span></span>
        </label>` : ''}
        <div class="field" style="margin:0">
          <label class="field-label" for="del-name">Type <strong>${esc(d.name)}</strong> to confirm</label>
          <input id="del-name" class="input input-mono" autocomplete="off" spellcheck="false" />
        </div>
      </div>
      <div class="dialog-actions">
        <button class="btn" id="del-cancel">Cancel</button>
        <button class="btn btn-danger" id="del-ok" disabled>Delete database</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  const ok = backdrop.querySelector('#del-ok');
  backdrop.querySelector('#del-name').addEventListener('input', e => { ok.disabled = e.target.value.trim() !== d.name; });
  backdrop.querySelector('#del-cancel').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  ok.onclick = async () => {
    ok.disabled = true;
    ok.textContent = 'Deleting…';
    const keep = !!backdrop.querySelector('#del-keep')?.checked;
    try {
      await api.deleteDatabase(DB_ID, keep, !d.node_online);
      location.href = '/#databases-section';
    } catch (e) {
      close();
      const force = await confirm('Remove from Cloudbase anyway?',
        `${esc(e.message)}<br><br>Cloudbase can forget this database, but anything left on node ${esc(d.node_name || '')} stays there.`);
      if (!force) return;
      try {
        await api.deleteDatabase(DB_ID, keep, true);
        location.href = '/#databases-section';
      } catch (e2) { toast(e2.message, 'error'); }
    }
  };
}
