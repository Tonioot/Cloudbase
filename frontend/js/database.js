import { api } from './api.js';
import { badge, toast, confirm, fmtUptime, stripAnsi, cssVar } from './utils.js';
import { esc, setCrumbs } from './shell.js';
import { engineIcon } from './engine-icons.js';
import { memoryWarning } from './databases.js';

const DB_ID = parseInt(new URLSearchParams(location.search).get('id'), 10);
const TRANSITIONAL = ['creating', 'starting', 'stopping'];
const TABS = ['overview', 'access', 'backups', 'logs', 'settings'];
const TRIGGER_LABEL = { manual: 'manual', scheduled: 'scheduled', pre_delete: 'before delete', pre_restore: 'before restore', pre_update: 'before update', upload: 'uploaded' };

const ICON = {
  copy: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  eye: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>',
  eyeOff: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 19c-7 0-11-7-11-7a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 7 11 7a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/></svg>',
  download: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>',
  restore: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><polyline points="3 3 3 8 8 8"/></svg>',
  remove: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
};
const MASK = '••••••••••••';

let database = null;
let credentials = null;     // fetched on demand; every fetch is audit-logged
let revealed = false;
let myIp = null;
let pollTimer = null;
let logsTimer = null;
let backups = null;       // last /backups response
let backupsTimer = null;
let verifyTls = (() => { try { return localStorage.getItem('cb-db-verify-tls') === '1'; } catch { return false; } })();

// MySQL and MariaDB encrypt with their own certificate, which the Cloudbase CA does not sign.
const canVerify = d => !['mysql', 'mariadb'].includes(d.engine);

function verifyHint(d) {
  const ca = '<a href="/api/databases/tls-ca.crt" download>cloudbase-database-ca.crt</a>';
  if (d.engine === 'redis' || d.engine === 'valkey') return `Download ${ca} and give it to your client, e.g. <code>redis-cli --tls --cacert cloudbase-database-ca.crt</code>.`;
  return `The copied URL checks the server with ${ca}. Save that file next to where you run your client.`;
}

// How well a public connection is protected, per engine.
function encryptionNote(d) {
  const ca = '<a href="/api/databases/tls-ca.crt" download>Download the Cloudbase CA</a> to also verify the server certificate.';
  if (d.engine === 'mariadb' && parseFloat(d.version) < 11.4) {
    return { warn: true, text: `MariaDB ${esc(d.version)} has no TLS out of the box, so connections stay unencrypted. Use MariaDB 11.4 or newer for encryption, or connect over an SSH tunnel.` };
  }
  if (!d.public_tls) {
    return { warn: true, text: d.engine === 'redis' || d.engine === 'valkey'
      ? `TLS is off: ${esc(d.engine_label)} sends the password and all data unencrypted. Only for clients that cannot do TLS, with a tight allowlist.`
      : 'TLS is off: queries and data travel unencrypted. Only for clients that cannot do TLS, with a tight allowlist.' };
  }
  if (d.engine === 'mysql' || d.engine === 'mariadb') {
    return { warn: false, text: `Encrypted end to end by ${esc(d.engine_label)} itself; clients that don't ask for TLS are refused. The URL contains <code>ssl-mode=REQUIRED</code>; some clients call it "require SSL".` };
  }
  const param = { postgres: '<code>sslmode=require</code>', mongodb: '<code>tls=true</code>', redis: '<code>rediss://</code>', valkey: '<code>rediss://</code>' }[d.engine];
  return { warn: false, text: `Encrypted with TLS; connections without it are refused. The URL already contains ${param}. ${ca}` };
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
  document.getElementById('lim-mem').addEventListener('input', () => syncMemoryWarning());
  document.getElementById('usage-range').addEventListener('click', e => {
    const btn = e.target.closest('[data-hours]');
    if (!btn) return;
    historyHours = Number(btn.dataset.hours);
    document.querySelectorAll('#usage-range [data-hours]').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    loadHistory();
  });
  window.addEventListener('resize', () => { if (usageHistory) drawHistory(); });
  setInterval(() => { if (document.getElementById('panel-overview').classList.contains('active')) loadHistory(); }, 60000);
  document.getElementById('btn-refresh-logs').onclick = loadLogs;
  document.getElementById('btn-delete').onclick = deleteDatabase;
  document.getElementById('btn-rotate').onclick = openRotateDialog;
  document.getElementById('btn-check-update').onclick = checkUpdate;
  document.getElementById('btn-update').onclick = updateEngine;
  document.getElementById('pub-enabled').addEventListener('change', togglePublic);
  document.getElementById('allow-form').addEventListener('submit', e => { e.preventDefault(); addAllowEntry(); });
  document.getElementById('allow-my-ip').onclick = () => addAllowEntry(myIp);
  document.getElementById('pub-tls').addEventListener('change', toggleTls);
  document.getElementById('btn-backup-now').onclick = backupNow;
  document.getElementById('backup-upload').addEventListener('change', uploadDump);
  document.getElementById('sch-hour').innerHTML = Array.from({ length: 24 }, (_, h) => `<option value="${h}">${String(h).padStart(2, '0')}:00</option>`).join('');
  ['sch-schedule', 'sch-hour', 'sch-retention'].forEach(id => document.getElementById(id).addEventListener('change', saveSchedule));
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
  clearTimeout(backupsTimer);
  if (name === 'logs') {
    loadLogs();
    logsTimer = setInterval(loadLogs, 5000);
  }
  if (name === 'backups') loadBackups();
  if (name === 'overview') loadHistory();
  if (name === 'settings') syncMemoryWarning(true);
}

let memoryContext = null;  // { node, databases } for the memory advice

async function syncMemoryWarning(refresh = false) {
  if (refresh || !memoryContext) {
    try {
      const [nodes, databases] = await Promise.all([api.listNodes(), api.listDatabases()]);
      memoryContext = { node: nodes.find(n => n.id === database.node_id), databases };
    } catch {
      return;
    }
  }
  const mb = parseInt(document.getElementById('lim-mem').value, 10) || 0;
  const text = memoryWarning(memoryContext.node, memoryContext.databases, mb, DB_ID);
  const el = document.getElementById('lim-mem-warning');
  el.textContent = text;
  el.style.display = text ? '' : 'none';
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
    `<span>${esc(d.engine_label)} ${esc(d.running_version || d.version)}</span><span>${esc(d.node_name || 'unknown node')}</span><code>${esc(d.host)}:${d.port}</code>`;
  document.getElementById('db-update-tag').style.display = d.update?.update_available ? '' : 'none';
  renderVersion();
  document.getElementById('db-status-badge').innerHTML = badge(d.status);
  document.getElementById('db-status-meta').textContent = d.progress || d.operation || (d.busy && !TRANSITIONAL.includes(d.status) ? 'Working…' : '');

  const err = document.getElementById('db-error');
  const showErr = d.last_error && ['error', 'unhealthy'].includes(d.status);
  err.style.display = showErr ? '' : 'none';
  err.textContent = showErr ? d.last_error : '';

  const stopped = d.desired_state === 'stopped';
  const blocked = d.busy || TRANSITIONAL.includes(d.status) || !d.node_online;
  document.getElementById('btn-start').style.display = stopped ? '' : 'none';
  document.getElementById('btn-stop').style.display = stopped ? 'none' : '';
  document.getElementById('btn-rotate').disabled = blocked || !['running', 'unhealthy'].includes(d.status);
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
      const ok = await confirm(`Unlink ${link.app_name}?`,
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
    kvRow('URL', verifyTls && credentials?.public_url_verified ? credentials.public_url_verified : credentials?.public_url,
      { copy: verifyTls && canVerify(d) ? 'public_url_verified' : 'public_url', secret: true }),
  ].join('') + (d.public_tls && canVerify(d) ? `
    <label class="toggle-row form-toggle db-tls-row">
      <span><span class="field-label">Verify the server certificate</span><span class="field-hint">${verifyTls ? verifyHint(d) : 'Off: the connection is encrypted, but your client does not check that it talks to this server.'}</span></span>
      <span class="toggle"><input type="checkbox" id="pub-verify" ${verifyTls ? 'checked' : ''} /><span class="toggle-slider"></span></span>
    </label>` : '');
  document.getElementById('pub-verify')?.addEventListener('change', e => {
    verifyTls = e.target.checked;
    try { localStorage.setItem('cb-db-verify-tls', verifyTls ? '1' : '0'); } catch {}
    renderAccess();
  });

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

  const tlsIn = document.getElementById('pub-tls');
  const noTls = d.engine === 'mariadb' && parseFloat(d.version) < 11.4;
  tlsIn.checked = !!d.public_tls && !noTls;
  tlsIn.disabled = noTls;
  document.getElementById('pub-tls-hint').textContent = noTls
    ? `Not available for MariaDB ${d.version}`
    : d.public_tls ? 'Connections without encryption are refused' : 'Off: unencrypted connections are allowed';

  const note = encryptionNote(d);
  const noteEl = document.getElementById('pub-note');
  noteEl.className = `db-note${note.warn ? ' db-note--warn' : ''}`;
  noteEl.innerHTML = `<p>${note.text}</p><p>Make TCP port <code>${d.public_port}</code> reachable on the primary: open it in its firewall or your cloud provider's security group, and forward it on your router if the server is behind one.</p>`;
}

async function savePublic(enabled, allowlist, message, tls = database.public_tls) {
  try {
    database = { ...database, ...(await api.setDatabasePublic(DB_ID, { enabled, allowlist, tls })) };
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

async function toggleTls(e) {
  const on = e.target.checked;
  if (!on) {
    const ok = await confirm('Turn TLS off?', 'Clients can then connect without encryption, so data travels readable over the internet. Only do this for a client that cannot use TLS.');
    if (!ok) { e.target.checked = true; return; }
  }
  await savePublic(database.public_enabled, database.public_allowlist || [], on ? 'TLS required' : 'TLS no longer required', on);
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
    const ok = await confirm(`${action === 'stop' ? 'Stop' : 'Restart'} ${database.name}?`,
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
    pre.textContent = lines.length ? stripAnsi(lines.join('\n'))
      : database?.progress ? `The container has not started yet.\n${database.progress}…`
      : 'No log output yet.';
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

/* ─── Usage history ─────────────────────────────────────────────────────── */

let historyHours = 24;
let usageHistory = null;

async function loadHistory() {
  try {
    usageHistory = (await api.getDatabaseStatsHistory(DB_ID, historyHours)).samples;
  } catch {
    return;
  }
  drawHistory();
}

function fmtMb(mb) {
  if (mb == null) return '—';
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

function drawHistory() {
  const has = usageHistory.length > 1;
  document.getElementById('usage-empty').style.display = has ? 'none' : '';
  const last = key => [...usageHistory].reverse().find(s => s[key] != null)?.[key];
  document.getElementById('u-cpu').textContent = has && last('cpu') != null ? `${last('cpu').toFixed(1)}%` : '';
  document.getElementById('u-mem').textContent = has ? fmtMb(last('mem')) : '';
  document.getElementById('u-disk').textContent = has ? fmtMb(last('disk')) : '';
  const memMax = database?.memory_limit_mb || null;
  drawChart('chart-cpu', usageHistory, 'cpu', '--accent', v => `${v.toFixed(0)}%`);
  drawChart('chart-mem', usageHistory, 'mem', '--purple', fmtMb, memMax);
  drawChart('chart-disk', usageHistory, 'disk', '--yellow', fmtMb);
}

/** A small area chart in the panel's chart style. */
function drawChart(id, samples, key, colorVar, fmt, fixedMax = null) {
  const canvas = document.getElementById(id);
  const dpr = devicePixelRatio || 1;
  canvas.width = canvas.offsetWidth * dpr;
  canvas.height = canvas.offsetHeight * dpr;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const pts = samples.filter(s => s[key] != null);
  if (pts.length < 2) return;

  const color = cssVar(colorVar);
  const raw = Math.max(...pts.map(p => p[key]));
  const max = fixedMax && fixedMax >= raw ? fixedMax : (raw <= 0 ? 1 : raw * 1.15);
  const pL = 4 * dpr, pR = 4 * dpr, pT = 6 * dpr, pB = 16 * dpr;
  const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts || t0 + 1;
  const x = t => pL + ((t - t0) / Math.max(t1 - t0, 1)) * (W - pL - pR);
  const y = v => pT + (1 - Math.min(v / max, 1)) * (H - pT - pB);

  ctx.strokeStyle = cssVar('--line');
  ctx.lineWidth = dpr;
  [0, 0.5, 1].forEach(f => {
    const gy = pT + f * (H - pT - pB);
    ctx.beginPath(); ctx.moveTo(pL, gy); ctx.lineTo(W - pR, gy); ctx.stroke();
  });

  ctx.beginPath();
  pts.forEach((p, i) => (i ? ctx.lineTo(x(p.ts), y(p[key])) : ctx.moveTo(x(p.ts), y(p[key]))));
  const line = new Path2D();
  pts.forEach((p, i) => (i ? line.lineTo(x(p.ts), y(p[key])) : line.moveTo(x(p.ts), y(p[key]))));
  ctx.lineTo(x(t1), H - pB);
  ctx.lineTo(x(t0), H - pB);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, pT, 0, H - pB);
  grad.addColorStop(0, color + '30');
  grad.addColorStop(1, color + '00');
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5 * dpr;
  ctx.lineJoin = 'round';
  ctx.stroke(line);

  ctx.fillStyle = cssVar('--faint');
  ctx.font = `${10 * dpr}px Geist, sans-serif`;
  ctx.textBaseline = 'bottom';
  const label = ts => {
    const d = new Date(ts);
    return historyHours > 24 ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
      : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  };
  ctx.textAlign = 'left';
  ctx.fillText(label(t0), pL, H);
  ctx.textAlign = 'right';
  ctx.fillText(label(t1), W - pR, H);
  ctx.textBaseline = 'top';
  ctx.fillText(fmt(max), W - pR, 0);
}

/* ─── Backups ───────────────────────────────────────────────────────────── */

function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${units[i]}`;
}

function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso.endsWith('Z') ? iso : `${iso}Z`);
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function backupRow(b, { own }) {
  const canRestore = backups.can_restore && b.status === 'success';
  const status = b.status === 'running'
    ? '<span class="db-tag db-tag--busy">running…</span>'
    : b.status === 'failed' ? `<span class="db-tag db-tag--failed" title="${esc(b.error || '')}">failed</span>` : '';
  return `
    <div class="db-list-row">
      <div class="db-list-main">
        <span class="db-list-title">${esc(fmtWhen(b.created_at))}</span>
        ${own ? '' : `<span class="db-list-meta">${esc(b.database_name)}${b.database_id ? '' : ' (deleted)'}</span>`}
        <span class="db-tag">${esc(TRIGGER_LABEL[b.trigger] || b.trigger)}</span>
        ${status}
        <span class="db-list-meta">${b.status === 'success' ? esc(fmtBytes(b.size_bytes)) : ''}</span>
        ${b.offsite_status === 'uploaded' ? '<span class="db-tag db-tag--offsite" title="A copy is in off-site storage">off-site</span>' : ''}
        ${b.offsite_status === 'failed' ? `<span class="db-tag db-tag--failed" title="${esc(b.offsite_error || '')}">off-site upload failed</span>` : ''}
        ${b.status === 'failed' && b.error ? `<span class="db-list-error">${esc(b.error.split('\n')[0].slice(0, 160))}</span>` : ''}
      </div>
      <span class="db-kv-actions">
        ${canRestore ? `<button type="button" class="icon-btn" data-restore="${b.id}" data-perm="databases.backup" title="Restore" aria-label="Restore this backup">${ICON.restore}</button>` : ''}
        ${own && b.status === 'success' ? `<a class="icon-btn" href="${api.backupDownloadUrl(DB_ID, b.id)}" download data-perm="databases.backup" title="Download" aria-label="Download this backup">${ICON.download}</a>` : ''}
        ${own && b.status !== 'running' ? `<button type="button" class="icon-btn" data-backup-delete="${b.id}" data-perm="databases.backup" title="Delete" aria-label="Delete this backup">${ICON.remove}</button>` : ''}
      </span>
    </div>`;
}

async function loadBackups() {
  clearTimeout(backupsTimer);
  try {
    backups = await api.listBackups(DB_ID);
  } catch (e) {
    document.getElementById('backup-list').innerHTML = `<div class="db-list-empty">${esc(e.message)}</div>`;
    return;
  }
  renderBackups();
  const active = backups.operation || backups.backups.some(b => b.status === 'running') || backups.restore?.state === 'running';
  if (document.getElementById('panel-backups').classList.contains('active')) {
    backupsTimer = setTimeout(loadBackups, active ? 2000 : 15000);
  }
  if (!active && database?.busy) load();  // refresh the header once an operation ends
}

function renderBackups() {
  const b = backups;
  const list = document.getElementById('backup-list');
  list.innerHTML = b.backups.length
    ? b.backups.map(x => backupRow(x, { own: true })).join('')
    : '<div class="db-list-empty">No backups yet.</div>';

  const others = document.getElementById('other-backups');
  others.style.display = b.can_restore && b.others.length ? '' : 'none';
  document.getElementById('other-backup-list').innerHTML = b.others.map(x => backupRow(x, { own: false })).join('');

  const hint = document.getElementById('offsite-hint');
  const settingsLink = '<a href="/settings?s=backups">Settings → Backup storage</a>';
  if (b.offsite?.enabled) {
    hint.className = 'db-offsite-hint db-offsite-hint--on';
    hint.innerHTML = `Every backup is also copied to the bucket <code>${esc(b.offsite.bucket || '')}</code>. Change this in ${settingsLink}.`;
  } else {
    hint.className = 'db-offsite-hint';
    hint.innerHTML = `Backups exist only on this server, so a broken disk loses them too. Add an S3, R2 or Backblaze bucket in ${settingsLink} to keep a copy elsewhere.`;
  }

  document.getElementById('backup-restore-note').textContent = b.can_restore ? '' :
    `Restoring ${database.engine_label} backups from the panel comes later. You can download a backup and load it yourself.`;

  const banner = document.getElementById('restore-banner');
  const r = b.restore;
  if (r) {
    banner.style.display = '';
    banner.className = `db-restore-banner db-restore-banner--${r.state}`;
    banner.textContent = r.state === 'running' ? (r.message || 'Restoring…')
      : r.state === 'success' ? `Restore finished (${fmtWhen(r.at)}).`
      : `Restore failed: ${r.message || 'unknown error'}`;
  } else {
    banner.style.display = 'none';
  }

  const busy = !!b.operation || !database.node_online || !['running', 'unhealthy'].includes(database.status);
  const now = document.getElementById('btn-backup-now');
  now.disabled = busy;
  now.textContent = b.operation === 'Backing up…' ? 'Backing up…' : 'Back up now';

  const sched = document.getElementById('sch-schedule');
  const hour = document.getElementById('sch-hour');
  const keep = document.getElementById('sch-retention');
  if (![sched, hour, keep].includes(document.activeElement)) {
    sched.value = b.schedule;
    hour.value = String(b.hour);
    keep.value = b.retention;
  }
  document.getElementById('sch-hour-field').style.display = sched.value === 'daily' ? '' : 'none';
  const last = b.backups.find(x => x.trigger === 'scheduled' && x.status === 'success');
  document.getElementById('schedule-state').textContent = b.schedule === 'off' ? 'Off'
    : last ? `Last automatic backup ${fmtWhen(last.created_at)}` : 'No automatic backup yet';

  document.querySelectorAll('[data-restore]').forEach(btn => {
    btn.onclick = () => openRestoreDialog([...b.backups, ...b.others].find(x => x.id === +btn.dataset.restore));
  });
  document.querySelectorAll('[data-backup-delete]').forEach(btn => {
    btn.onclick = () => deleteBackup(b.backups.find(x => x.id === +btn.dataset.backupDelete));
  });
  if (window._applyPermVisibility) window._applyPermVisibility(document.getElementById('panel-backups'));
}

async function backupNow() {
  try {
    await api.createBackup(DB_ID);
    toast('Backup started');
    loadBackups();
  } catch (e) { toast(e.message, 'error'); }
}

async function uploadDump(e) {
  const file = e.target.files[0];
  e.target.value = '';
  if (!file) return;
  const label = document.getElementById('btn-upload-text');
  try {
    await api.uploadBackup(DB_ID, file, p => { label.textContent = `Uploading ${Math.round(p * 100)}%`; });
    toast(`${file.name} uploaded. Restore it from the list.`);
    loadBackups();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    label.textContent = 'Upload dump';
  }
}

async function saveSchedule() {
  const schedule = document.getElementById('sch-schedule').value;
  document.getElementById('sch-hour-field').style.display = schedule === 'daily' ? '' : 'none';
  try {
    await api.updateBackupSettings(DB_ID, {
      schedule,
      hour: parseInt(document.getElementById('sch-hour').value, 10),
      retention: Math.max(1, parseInt(document.getElementById('sch-retention').value, 10) || 7),
    });
    toast('Backup schedule saved');
    loadBackups();
  } catch (e) { toast(e.message, 'error'); }
}

async function deleteBackup(b) {
  const ok = await confirm('Delete this backup?', `The backup of ${esc(fmtWhen(b.created_at))} (${esc(fmtBytes(b.size_bytes))}) is removed for good.`);
  if (!ok) return;
  try {
    await api.deleteBackup(DB_ID, b.id);
    toast('Backup deleted');
    loadBackups();
  } catch (e) { toast(e.message, 'error'); }
}

function openRestoreDialog(b) {
  const d = database;
  const from = b.database_id === DB_ID ? '' : ` of <strong>${esc(b.database_name)}</strong>`;
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="rs-title">
      <div class="dialog-title" id="rs-title">Restore ${esc(d.name)}</div>
      <div class="dialog-body">
        <p class="form-dialog-intro">Loads the backup${from} from <strong>${esc(fmtWhen(b.created_at))}</strong> into <strong>${esc(d.name)}</strong>. Data in the backup replaces the current data; linked apps keep running but see the restored data.</p>
        <label class="toggle-row form-toggle">
          <span><span class="field-label">Make a safety backup first</span><span class="field-hint">So you can go back to the current data</span></span>
          <span class="toggle"><input type="checkbox" id="rs-safety" checked /><span class="toggle-slider"></span></span>
        </label>
        <div class="field" style="margin:0">
          <label class="field-label" for="rs-name">Type <strong>${esc(d.name)}</strong> to confirm</label>
          <input id="rs-name" class="input input-mono" autocomplete="off" spellcheck="false" />
        </div>
      </div>
      <div class="dialog-actions">
        <button class="btn" id="rs-cancel">Cancel</button>
        <button class="btn btn-danger" id="rs-ok" disabled>Restore</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  const ok = backdrop.querySelector('#rs-ok');
  backdrop.querySelector('#rs-name').addEventListener('input', e => { ok.disabled = e.target.value.trim() !== d.name; });
  backdrop.querySelector('#rs-cancel').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  ok.onclick = async () => {
    ok.disabled = true;
    try {
      await api.restoreBackup(DB_ID, b.id, backdrop.querySelector('#rs-safety').checked);
      close();
      toast('Restore started');
      loadBackups();
      load();
    } catch (e) {
      toast(e.message, 'error');
      ok.disabled = false;
    }
  };
}

/* ─── Version / updates ─────────────────────────────────────────────────── */

function renderVersion() {
  const d = database;
  const u = d.update;
  const sub = document.getElementById('ver-sub');
  const current = `${d.engine_label} ${u?.version || d.version}`;
  const when = u?.checked_at ? timeAgoShort(u.checked_at) : null;
  if (!u) sub.textContent = `${current}. Not checked for updates yet.`;
  else if (u.error && !u.update_available) sub.textContent = `${current}. Last check failed: ${u.error}`;
  else if (u.update_available) {
    sub.textContent = `${current}: a newer ${d.version}.x release${u.latest_version ? ` (${u.latest_version})` : ''} is available. Updating makes a backup first, keeps your data and rolls back by itself if the new version doesn't start.`;
  } else {
    sub.textContent = `${current}, the newest ${d.version}.x release${when ? ` (checked ${when})` : ''}${u.updated_from ? `. Updated from ${u.updated_from}.` : '.'}`;
  }
  const btn = document.getElementById('btn-update');
  btn.style.display = u?.update_available ? '' : 'none';
  btn.disabled = d.busy || !d.node_online || !['running', 'unhealthy'].includes(d.status);
}

function timeAgoShort(iso) {
  const s = (Date.now() - new Date(iso.endsWith('Z') ? iso : `${iso}Z`).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

async function checkUpdate() {
  const btn = document.getElementById('btn-check-update');
  btn.disabled = true;
  btn.textContent = 'Checking…';
  try {
    database.update = await api.checkDatabaseUpdate(DB_ID);
    render();
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check now';
  }
}

async function updateEngine() {
  const d = database;
  const ok = await confirm(`Update ${d.name}?`,
    `Installs the newest ${esc(d.engine_label)} ${esc(d.version)}.x release. A backup is made first, the database restarts briefly with the same data, and Cloudbase rolls back by itself if the new version doesn't start.`);
  if (!ok) return;
  try {
    await api.updateDatabaseEngine(DB_ID);
    toast('Update started');
    load();
  } catch (e) { toast(e.message, 'error'); }
}

/* ─── Dialogs ───────────────────────────────────────────────────────────── */

function openRotateDialog() {
  const d = database;
  const apps = (d.links || []).map(l => l.app_name);
  const recreates = ['mongodb', 'redis', 'valkey'].includes(d.engine);
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="rot-title">
      <div class="dialog-title" id="rot-title">Change the password of ${esc(d.name)}</div>
      <div class="dialog-body">
        <p class="form-dialog-intro">A new random password replaces the current one. Anything still using the old password loses access${recreates ? `, and ${esc(d.engine_label)} restarts briefly to pick it up` : ''}.</p>
        ${apps.length ? `
        <label class="toggle-row form-toggle">
          <span><span class="field-label">Restart linked apps now</span><span class="field-hint">${esc(apps.join(', '))} get the new URL when they restart</span></span>
          <span class="toggle"><input type="checkbox" id="rot-restart" checked /><span class="toggle-slider"></span></span>
        </label>` : '<p class="form-dialog-intro">No apps are linked. Update any external clients (pgAdmin, scripts) yourself.</p>'}
      </div>
      <div class="dialog-actions">
        <button class="btn" id="rot-cancel">Cancel</button>
        <button class="btn btn-danger" id="rot-ok">Change password</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  backdrop.querySelector('#rot-cancel').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  const ok = backdrop.querySelector('#rot-ok');
  ok.onclick = async () => {
    ok.disabled = true;
    ok.textContent = 'Changing…';
    const restart = !!backdrop.querySelector('#rot-restart')?.checked;
    try {
      const res = await api.rotateDatabasePassword(DB_ID);
      credentials = null;
      revealed = false;
      close();
      if (restart && res.apps.length) {
        const failed = [];
        for (const app of res.apps) {
          try { await api.restart(app.id); } catch { failed.push(app.name); }
        }
        toast(failed.length ? `Password changed. Restart ${failed.join(', ')} yourself.` : `Password changed; restarting ${res.apps.map(a => a.name).join(', ')}.`, failed.length ? 'error' : 'success');
      } else {
        toast(res.apps.length ? `Password changed. Restart ${res.apps.map(a => a.name).join(', ')} to use it.` : 'Password changed');
      }
      load();
    } catch (e) {
      toast(e.message, 'error');
      ok.disabled = false;
      ok.textContent = 'Change password';
    }
  };
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
        ${d.node_online && ['running', 'unhealthy'].includes(d.status) ? `
        <label class="toggle-row form-toggle">
          <span><span class="field-label">Make a final backup first</span><span class="field-hint">Kept on the primary after the database is gone</span></span>
          <span class="toggle"><input type="checkbox" id="del-backup" checked /><span class="toggle-slider"></span></span>
        </label>` : ''}
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
    const keep = !!backdrop.querySelector('#del-keep')?.checked;
    const finalBackup = !!backdrop.querySelector('#del-backup')?.checked;
    ok.textContent = finalBackup ? 'Backing up…' : 'Deleting…';
    try {
      await api.deleteDatabase(DB_ID, keep, !d.node_online, finalBackup);
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
