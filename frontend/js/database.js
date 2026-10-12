import { api } from './api.js';
import { badge, toast, confirm, fmtUptime } from './utils.js';
import { esc, setCrumbs } from './shell.js';
import { engineIcon } from './engine-icons.js';

const DB_ID = parseInt(new URLSearchParams(location.search).get('id'), 10);
const TRANSITIONAL = ['creating', 'starting', 'stopping'];
const TABS = ['overview', 'access', 'logs', 'settings'];

const ICON = {
  copy: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  eye: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  eyeOff: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 19c-7 0-11-7-11-7a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 7 11 7a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
  remove: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
};
const MASK = '••••••••••••';

let database = null;
let credentials = null;     // fetched on demand; every fetch is audit-logged
let revealed = false;
let myIp = null;
let pollTimer = null;
let logsTimer = null;

// How well a public connection is protected, per engine.
function encryptionNote(d) {
  const tlsDefault = d.engine === 'mysql' || (d.engine === 'mariadb' && parseFloat(d.version) >= 11.4);
  if (tlsDefault) return { warn: false, text: `${esc(d.engine_label)} encrypts connections with its own self-signed certificate. Use <code>ssl-mode=REQUIRED</code> (or "require SSL") in your client.` };
  if (d.engine === 'postgres' || d.engine === 'mongodb') return { warn: false, text: 'The login is protected (SCRAM), so the password is never sent readable, but queries and data are not encrypted. Keep the allowlist tight.' };
  return { warn: true, text: `${esc(d.engine_label)} sends the password and all data unencrypted. Only use this with a tight allowlist, or connect over an SSH tunnel instead.` };
}

export async function initDatabasePage() {
  if (!DB_ID) { location.href = '/'; return; }

  document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
    btn.addEventListener('click', () => showTab(btn.dataset.tab));
  });
  document.getElementById('btn-start').onclick = () => lifecycle('start');
  document.getElementById('btn-stop').onclick = () => lifecycle('stop');
  document.getElementById('btn-restart').onclick = () => lifecycle('restart');
  document.getElementById('btn-link').onclick = openLinkDialog;
  document.getElementById('limits-form').addEventListener('submit', e => { e.preventDefault(); saveLimits(); });
  document.getElementById('btn-refresh-logs').onclick = loadLogs;
  document.getElementById('btn-delete').onclick = deleteDatabase;
  document.getElementById('pub-enabled').addEventListener('change', togglePublic);
  document.getElementById('allow-form').addEventListener('submit', e => { e.preventDefault(); addAllowEntry(); });
  document.getElementById('allow-my-ip').onclick = () => addAllowEntry(myIp);
  // Copy / reveal buttons are rendered into the key-value rows.
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-copy], [data-reveal]');
    if (!btn) return;
    if (btn.dataset.reveal !== undefined) toggleReveal();
    else copyValue(btn.dataset.copy);
  });

  api.getMyIp().then(r => { myIp = r.ip || null; if (database) renderAccess(); }).catch(() => {});
  await load();
  showTab(TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview', false);
  loadStats();
  setInterval(loadStats, 10000);
}

/* ─── Tabs ──────────────────────────────────────────────────────────────── */

function showTab(name, updateHash = true) {
  document.querySelectorAll('.tab-btn[data-tab]').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  TABS.forEach(t => document.getElementById(`panel-${t}`).classList.toggle('active', t === name));
  if (updateHash) history.replaceState(null, '', `#${name}`);
  clearInterval(logsTimer);
  if (name === 'logs') {
    loadLogs();
    logsTimer = setInterval(loadLogs, 5000);
  }
}

/* ─── Data ──────────────────────────────────────────────────────────────── */

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

async function getCredentials() {
  if (!credentials) credentials = await api.getDatabaseCredentials(DB_ID);
  return credentials;
}

/* ─── Rendering ─────────────────────────────────────────────────────────── */

function kvRow(label, value, { copy = null, secret = false } = {}) {
  const shown = secret && !revealed ? MASK : value;
  return `
    <div class="db-kv-row">
      <span class="db-kv-label">${esc(label)}</span>
      <code class="db-kv-value${secret && !revealed ? ' masked' : ''}">${esc(shown ?? '—')}</code>
      <span class="db-kv-actions">
        ${secret ? `<button type="button" class="icon-btn" data-reveal data-perm="databases.credentials" title="${revealed ? 'Hide' : 'Show'}" aria-label="${revealed ? 'Hide' : 'Show'} ${esc(label.toLowerCase())}">${revealed ? ICON.eyeOff : ICON.eye}</button>` : ''}
        ${copy ? `<button type="button" class="icon-btn" data-copy="${esc(copy)}" ${secret ? 'data-perm="databases.credentials"' : ''} title="Copy" aria-label="Copy ${esc(label.toLowerCase())}">${ICON.copy}</button>` : ''}
      </span>
    </div>`;
}

function render() {
  const d = database;
  document.title = `Cloudbase — ${d.name}`;
  setCrumbs([{ label: 'Databases', href: '/#databases-section' }, d.name]);

  document.getElementById('db-name').textContent = d.name;
  document.getElementById('db-engine-icon').innerHTML = engineIcon(d.engine, 36);
  document.getElementById('db-meta').innerHTML =
    `<span>${esc(d.engine_label)} ${esc(d.version)}</span><span>${esc(d.node_name || 'unknown node')}</span><code>${esc(d.host)}:${d.port}</code>`;
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
  document.getElementById('s-links').textContent = d.link_count || '0';

  document.getElementById('conn-rows').innerHTML = [
    kvRow('Host', d.host, { copy: 'host' }),
    kvRow('Port', d.port, { copy: 'port' }),
    kvRow('Database', d.db_name, { copy: 'db_name' }),
    kvRow('User', d.user, { copy: 'user' }),
    kvRow('Password', credentials?.password, { copy: 'password', secret: true }),
    kvRow('URL', credentials?.url, { copy: 'url', secret: true }),
  ].join('');

  const cpuIn = document.getElementById('lim-cpu');
  const memIn = document.getElementById('lim-mem');
  if (document.activeElement !== cpuIn) cpuIn.value = d.cpu_limit ?? '';
  if (document.activeElement !== memIn) memIn.value = d.memory_limit_mb ?? '';

  renderLinks();
  renderAccess();
  if (window._applyPermVisibility) window._applyPermVisibility(document.body);
}

function renderLinks() {
  const list = document.getElementById('links-list');
  const links = database.links || [];
  if (!links.length) {
    list.innerHTML = '<div class="db-list-empty">No apps linked yet.</div>';
    return;
  }
  list.innerHTML = links.map(l => `
    <div class="db-list-row">
      <div class="db-list-main">
        <a href="/app?id=${l.app_id}" class="db-list-title">${esc(l.app_name)}</a>
        <code class="db-list-meta">${esc(l.env_var_name)}</code>
        ${l.instances_elsewhere ? '<span class="db-tag" title="Instances on other nodes connect through the primary (a few ms extra per query)">via primary</span>' : ''}
      </div>
      <button type="button" class="icon-btn" data-unlink="${l.id}" data-perm="databases.manage" title="Unlink" aria-label="Unlink ${esc(l.app_name)}">${ICON.remove}</button>
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

/* ─── Access ────────────────────────────────────────────────────────────── */

function describeEntry(entry) {
  const m = entry.match(/\/(\d+)$/);
  if (!m) return 'Single address';
  const bits = parseInt(m[1], 10);
  const total = (entry.includes(':') ? 128 : 32) - bits;
  if (total === 0) return 'Single address';
  return total > 24 ? 'Large range' : `Range of ${(2 ** total).toLocaleString()} addresses`;
}

function renderAccess() {
  const d = database;
  const on = !!d.public_enabled;
  document.getElementById('pub-enabled').checked = on;
  document.getElementById('pub-on').style.display = on ? '' : 'none';
  document.getElementById('pub-intro').style.display = on ? 'none' : '';
  if (!on) return;

  const endpoint = d.public_host ? `${d.public_host}:${d.public_port}` : `<primary>:${d.public_port}`;
  document.getElementById('pub-rows').innerHTML = [
    d.public_listening
      ? kvRow('Address', endpoint, { copy: 'public_endpoint' })
      : `<div class="db-kv-row"><span class="db-kv-label">Address</span><span class="db-kv-warn">Port ${d.public_port} could not be opened on the primary. Check the server log.</span></div>`,
    kvRow('URL', credentials?.public_url, { copy: 'public_url', secret: true }),
  ].join('');

  const entries = d.public_allowlist || [];
  document.getElementById('allow-count').textContent = entries.length ? `${entries.length} allowed` : '';
  document.getElementById('allow-list').innerHTML = entries.length
    ? entries.map(entry => `
        <div class="db-list-row">
          <div class="db-list-main">
            <code class="db-list-title">${esc(entry)}</code>
            ${entry === myIp ? '<span class="db-tag db-tag--you">you</span>' : ''}
            <span class="db-list-meta">${esc(describeEntry(entry))}</span>
          </div>
          <button type="button" class="icon-btn" data-allow-remove="${esc(entry)}" data-perm="databases.manage" title="Remove" aria-label="Remove ${esc(entry)}">${ICON.remove}</button>
        </div>`).join('')
    : '<div class="db-list-empty db-list-empty--warn">No restrictions: anyone on the internet can try to log in.</div>';
  document.querySelectorAll('[data-allow-remove]').forEach(btn => {
    btn.onclick = () => removeAllowEntry(btn.dataset.allowRemove);
  });

  const myBtn = document.getElementById('allow-my-ip');
  myBtn.textContent = myIp ? `Add my IP (${myIp})` : 'Add my IP';
  myBtn.disabled = !myIp || entries.includes(myIp);
  document.getElementById('allow-hint').textContent = myIp
    ? `Your IP as seen by this panel is ${myIp}. If your database client connects through a VPN or another network (IPv4 or IPv6), add that address too.`
    : '';

  const note = encryptionNote(d);
  const noteEl = document.getElementById('pub-note');
  noteEl.className = `db-note${note.warn ? ' db-note--warn' : ''}`;
  noteEl.innerHTML = `<p>${note.text}</p><p>Make TCP port <code>${d.public_port}</code> reachable on the primary: open it in its firewall or your cloud provider's security group, and forward it on your router if the server is behind one.</p>`;
}

async function savePublic(enabled, allowlist, message) {
  try {
    database = { ...database, ...(await api.setDatabasePublic(DB_ID, { enabled, allowlist })) };
    credentials = null;
    revealed = false;
    render();
    if (message) toast(message);
    return true;
  } catch (e) {
    toast(e.message, 'error');
    render();
    return false;
  }
}

async function togglePublic(e) {
  const enable = e.target.checked;
  let list = database.public_allowlist || [];
  if (enable && !list.length && myIp) {
    // Safe default: start with only this browser's address.
    list = [myIp];
    await savePublic(true, list, `Public access on. Only ${myIp} is allowed; add more addresses below.`);
    return;
  }
  if (enable && !list.length) {
    const ok = await confirm('Open to everyone?', 'Without an allowlist anyone on the internet can try to log in to this database. The password is still required.');
    if (!ok) { e.target.checked = false; return; }
  }
  await savePublic(enable, list, enable ? 'Public access on' : 'Public access off');
}

function looksLikeIp(value) {
  const v4 = /^(\d{1,3}\.){3}\d{1,3}(\/(3[0-2]|[12]?\d))?$/;
  const v6 = /^[0-9a-fA-F:]+:[0-9a-fA-F:.]*(\/(12[0-8]|1[01]\d|\d{1,2}))?$/;
  return (v4.test(value) && value.split('/')[0].split('.').every(n => +n <= 255)) || v6.test(value);
}

async function addAllowEntry(value) {
  const input = document.getElementById('allow-input');
  const entry = (value ?? input.value).trim();
  if (!entry) return;
  if (!looksLikeIp(entry)) { toast(`"${entry}" is not an IP address or range`, 'error'); return; }
  const list = database.public_allowlist || [];
  if (list.includes(entry)) { toast(`${entry} is already allowed`); return; }
  if (await savePublic(database.public_enabled, [...list, entry], `${entry} allowed`)) input.value = '';
}

async function removeAllowEntry(entry) {
  const list = (database.public_allowlist || []).filter(e => e !== entry);
  if (!list.length && database.public_enabled) {
    const ok = await confirm('Remove the last address?', 'With an empty allowlist anyone on the internet can try to log in. Turn public access off instead if you no longer need it.');
    if (!ok) return;
  }
  await savePublic(database.public_enabled, list, `${entry} removed`);
}

/* ─── Copy / reveal ─────────────────────────────────────────────────────── */

async function toggleReveal() {
  if (!revealed) {
    try { await getCredentials(); } catch (e) { toast(e.message, 'error'); return; }
  }
  revealed = !revealed;
  render();
}

async function copyValue(key) {
  const d = database;
  const plain = {
    host: d.host, port: String(d.port), db_name: d.db_name, user: d.user,
    public_endpoint: d.public_host ? `${d.public_host}:${d.public_port}` : String(d.public_port),
  };
  try {
    const value = key in plain ? plain[key] : (await getCredentials())[key];
    if (!value) { toast('Nothing to copy', 'error'); return; }
    await navigator.clipboard.writeText(value);
    toast('Copied');
  } catch (e) { toast(e.message, 'error'); }
}

/* ─── Actions ───────────────────────────────────────────────────────────── */

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

async function loadStats() {
  if (!database || database.status === 'node_offline') return;
  const set = (id, v) => { document.getElementById(id).textContent = v; };
  try {
    const s = await api.getDatabaseStats(DB_ID);
    if (!s || s.cpu_percent === undefined) { ['s-cpu', 's-mem', 's-disk', 's-uptime'].forEach(id => set(id, '—')); return; }
    const limit = database.memory_limit_mb ? ` / ${database.memory_limit_mb}` : '';
    set('s-cpu', `${s.cpu_percent.toFixed(1)}%`);
    set('s-mem', `${Math.round(s.memory_mb)}${limit} MB`);
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

/* ─── Dialogs ───────────────────────────────────────────────────────────── */

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
