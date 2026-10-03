import { api, wsNodeEvents, PermissionError } from './api.js';
import { icon, toast, confirm } from './utils.js';
import { openDeployModal } from './modal.js';
import { statusColor, esc } from './shell.js';

let appsData = [];
let nodesData = [];
let _noAppsPermission  = false;
let _noNodesPermission = false;
let _appFilter = 'all';
const _appStats = new Map();      // app id → { cpu, mem }
const _nodePing = new Map();      // node id → latency text
const _pingIntervals = new Map();

// Sparkline series — sampled by the server in the background (/api/overview/history)
// so they are complete the moment the dashboard opens.
const HISTORY_LEN = 60;
const _history = { apps: [], instances: [], cpu: [], mem: [], nodes: [] };

/* ─── Init ──────────────────────────────────────────────────────────────── */
export async function initDashboard() {
  const openDeploy = () => openDeployModal(app => {
    toast(`"${app.name}" deployed successfully`);
    window.location.href = `/app?id=${app.id}`;
  });
  document.getElementById('btn-deploy').addEventListener('click', openDeploy);
  document.getElementById('btn-add-node')?.addEventListener('click', () => openAddNodeModal());

  document.getElementById('apps-filter')?.addEventListener('click', e => {
    const btn = e.target.closest('[data-filter]');
    if (!btn) return;
    _appFilter = btn.dataset.filter;
    document.querySelectorAll('#apps-filter [data-filter]').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    renderApps();
  });

  // Deep links from the command palette on other pages
  const action = new URLSearchParams(location.search).get('action');
  if (action) {
    history.replaceState(null, '', '/');
    if (action === 'new-app') openDeploy();
    if (action === 'add-node') openAddNodeModal();
  }

  // Load independently — a 403 on one must not block the other
  await Promise.all([loadApps(), loadNodes()]);
  loadAppStats();
  await loadOverviewHistory();
  setInterval(loadOverviewHistory, 30000);
  if (!_noAppsPermission)  { setInterval(loadApps, 6000); setInterval(loadAppStats, 15000); }
  if (!_noNodesPermission) setInterval(loadNodes, 15000);
}

/* ─── Data ──────────────────────────────────────────────────────────────── */
async function loadApps() {
  try {
    appsData = await api.listApps();
    renderKpis();
    renderApps();
    if (nodesData.length) renderNodes();
  } catch (e) {
    if (e instanceof PermissionError) {
      _noAppsPermission = true;
      document.getElementById('apps-section')?.closest('section')?.style.setProperty('display', 'none');
      document.querySelector('.stat-strip')?.style.setProperty('display', 'none');
    } else {
      console.error('Failed to load apps:', e);
    }
  }
}

async function loadNodes() {
  try {
    nodesData = await api.listNodes();
    renderNodes();
    renderKpis();
  } catch (e) {
    if (e instanceof PermissionError) {
      _noNodesPermission = true;
      const section = document.getElementById('nodes-section');
      if (section) section.style.display = 'none';
    } else {
      console.error('Failed to load nodes:', e);
    }
  }
}

// CPU / memory per app, summed over its instances. Only for running apps.
async function loadAppStats() {
  const running = appsData.filter(a => a.status === 'running');
  await Promise.all(running.map(async a => {
    try {
      const snap = await api.getInstanceStats(a.id);
      const rows = Object.values(snap || {});
      if (!rows.length) return;
      _appStats.set(a.id, {
        cpu: rows.reduce((s, r) => s + (r.cpu_percent || 0), 0),
        mem: rows.reduce((s, r) => s + (r.memory_mb || 0), 0),
      });
    } catch { /* stats are best-effort */ }
  }));
  for (const id of [..._appStats.keys()]) {
    if (!running.some(a => a.id === id)) _appStats.delete(id);
  }
  renderApps();
}

/* ─── Helpers ───────────────────────────────────────────────────────────── */
const KINDS = { nodejs: 'Node.js', python: 'Python', go: 'Go', ruby: 'Ruby', php: 'PHP', java: 'Java', dotnet: '.NET', rust: 'Rust' };

function appCategory(app) {
  if (app.no_web) return 'worker';
  if (app.app_type === 'static') return 'static';
  return 'web';
}

function appKind(app) {
  if (app.no_web) return 'Worker';
  if (app.app_type === 'static') return 'Static site';
  return KINDS[app.app_type] || 'Web service';
}

function nodeLabel(n) {
  return n ? (n.is_local ? 'primary' : n.name) : 'primary';
}

function replicasOnNode(app, node) {
  return (app.replicas || []).filter(r =>
    node.is_local ? (r.node_id === node.id || r.node_id == null) : r.node_id === node.id);
}

function fmtMem(mb) {
  if (mb == null) return '—';
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

async function loadOverviewHistory() {
  try {
    const { samples = [] } = await api.getOverviewHistory();
    const recent = samples.slice(-HISTORY_LEN);
    const pick = key => recent.map(x => x[key]).filter(v => v != null);
    _history.apps = pick('apps_running');
    _history.instances = pick('instances');
    _history.cpu = pick('cpu');
    _history.mem = pick('mem_used_mb');
    _history.nodes = pick('nodes_online');
    renderKpis();
  } catch { /* sparklines are optional */ }
}

function sparkPoints(values, w = 96, h = 18, pad = 2) {
  if (values.length < 2) return '';
  const max = Math.max(...values), min = Math.min(...values);
  if (max === min) return '';  // no variation yet: a flat line carries no information
  const span = max - min;
  return values.map((v, i) =>
    `${(i * w / (values.length - 1)).toFixed(1)},${(h - pad - ((v - min) / span) * (h - pad * 2)).toFixed(1)}`).join(' ');
}

function setKpi(id, value, unit, histKey) {
  const el = document.getElementById(id);
  if (!el) return;
  el.querySelector('.kpi-value strong').textContent = value;
  if (unit != null) el.querySelector('.kpi-value span').textContent = unit;
  el.querySelector('.kpi-spark polyline').setAttribute('points', sparkPoints(_history[histKey]));
}

/* ─── Key numbers ───────────────────────────────────────────────────────── */
function renderKpis() {
  const running = appsData.filter(a => a.status === 'running').length;
  const instances = appsData.reduce((s, a) => s + (a.replicas || []).filter(r => r.status === 'running').length, 0);
  const online = nodesData.filter(n => n.status === 'online');

  // Cluster CPU = average over online nodes; memory = used / total in GB
  const cpuVals = online.map(n => n.node_metrics?.cpu_percent).filter(v => v != null);
  const cpu = cpuVals.length ? cpuVals.reduce((a, b) => a + b, 0) / cpuVals.length : null;
  let memUsed = 0, memTotal = 0;
  for (const n of online) {
    const total = n.metadata?.ram_total_mb;
    const pct = n.node_metrics?.memory_percent;
    if (total && pct != null) { memTotal += total; memUsed += total * pct / 100; }
  }

  setKpi('kpi-apps', String(running), `of ${appsData.length}`, 'apps');
  setKpi('kpi-instances', String(instances), null, 'instances');
  setKpi('kpi-cpu', cpu != null ? cpu.toFixed(0) : '—', null, 'cpu');
  setKpi('kpi-mem', memTotal ? (memUsed / 1024).toFixed(1) : '—', memTotal ? `of ${(memTotal / 1024).toFixed(0)} GB` : '', 'mem');
  setKpi('kpi-nodes', nodesData.length ? String(online.length) : '—', nodesData.length ? `of ${nodesData.length} online` : 'online', 'nodes');

  const meta = document.getElementById('overview-meta');
  if (meta) {
    const parts = [];
    if (nodesData.length) parts.push(`${nodesData.length} node${nodesData.length === 1 ? '' : 's'}`);
    parts.push(`${appsData.length} app${appsData.length === 1 ? '' : 's'}`);
    meta.textContent = parts.join(' · ');
  }
}

/* ─── Nodes ─────────────────────────────────────────────────────────────── */
function meterHTML(label, pct) {
  if (pct == null) return '';
  const p = Math.max(0, Math.min(100, pct));
  const level = p >= 90 ? 'crit' : p >= 80 ? 'warn' : '';
  return `<div class="node-meter">
    <span class="node-meter-label">${label}</span>
    <div class="meter ${level}"><span style="width:${p}%"></span></div>
    <span class="node-meter-value ${level}">${p.toFixed(0)}%</span>
  </div>`;
}

function nodeRowHTML(n) {
  const online = n.status === 'online';
  const m = n.node_metrics || {};
  const md = n.metadata || {};
  const metaParts = [
    md.arch,
    n.is_local ? 'local' : (n.websocket_connected ? 'tunnel' : n.status),
    md.ip || md.public_ip || n.public_host,
    _nodePing.get(n.id),
    n.agent_version ? `agent ${n.agent_version}` : null,
  ].filter(Boolean);

  const apps = appsData
    .map(a => ({ app: a, reps: replicasOnNode(a, n) }))
    .filter(x => x.reps.length);
  const chips = apps.length
    ? apps.map(({ app, reps }) => {
        const up = reps.filter(r => r.status === 'running').length;
        return `<a class="app-chip" href="/app?id=${app.id}"><span class="dot" style="background:${statusColor(app.status)}"></span>${esc(app.name)}<span class="app-chip-count">×${up}</span></a>`;
      }).join('')
    : '<span class="node-apps-empty">No apps on this node</span>';

  const meters = online
    ? meterHTML('CPU', m.cpu_percent) + meterHTML('Memory', m.memory_percent) + meterHTML('Disk', m.disk_percent)
    : `<span class="node-offline-note">${n.status === 'offline' ? `Offline · last seen ${timeAgo(n.last_seen)}` : 'Connecting…'}</span>`;

  const removeBtn = n.is_local ? '' : `
    <button type="button" class="row-action node-row-remove" data-perm="nodes.delete" data-node-delete="${n.id}" data-node-name="${esc(n.name)}" aria-label="Remove node ${esc(n.name)}" title="Remove node">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
    </button>`;

  return `
    <div class="node-row" data-node-id="${n.id}">
      <div class="node-row-id">
        <span class="node-row-title">
          <span class="dot" style="background:${statusColor(n.status)};box-shadow:0 0 0 4px ${online ? 'var(--green-halo)' : 'transparent'}"></span>
          <a class="node-row-name" href="/node?id=${n.id}">${esc(nodeLabel(n))}</a>
          ${n.is_local ? '<span class="node-row-tag">primary</span>' : ''}
        </span>
        <span class="node-row-meta">${esc(metaParts.join(' · '))}</span>
      </div>
      <div class="node-meters">${meters}</div>
      <div class="node-apps">${chips}</div>
      ${removeBtn}
    </div>`;
}

function renderNodes() {
  const section = document.getElementById('nodes-section');
  const list = document.getElementById('nodes-grid');
  if (!list || (section && section.style.display === 'none')) return;

  if (!nodesData.length) {
    list.innerHTML = '<div class="apps-empty"><strong>No nodes yet</strong>Add a node to run apps on another server.</div>';
    return;
  }

  list.innerHTML = nodesData.map(nodeRowHTML).join('');
  if (window._applyPermVisibility) window._applyPermVisibility(list);

  list.querySelectorAll('.node-row').forEach(row => {
    row.addEventListener('click', e => {
      if (e.target.closest('a, button')) return;
      location.href = `/node?id=${row.dataset.nodeId}`;
    });
  });

  list.querySelectorAll('[data-node-delete]').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      const id   = parseInt(btn.dataset.nodeDelete, 10);
      const name = btn.dataset.nodeName || 'this node';
      const ok = await confirm(`Remove node "${name}"?`, 'This cannot be undone.');
      if (!ok) return;
      btn.disabled = true;
      try {
        await api.deleteNode(id);
        await loadNodes();
        toast(`Node "${name}" removed`);
      } catch (err) {
        toast(err.message, 'error');
        btn.disabled = false;
      }
    });
  });

  // Latency for remote nodes; results are shown in the meta line on the next render
  for (const n of nodesData) {
    if (n.is_local || n.status !== 'online' || _pingIntervals.has(n.id)) continue;
    const ping = async () => {
      try {
        const r = await api.pingNode(n.id);
        _nodePing.set(n.id, r.reachable ? `${r.latency_ms} ms` : 'unreachable');
      } catch { _nodePing.delete(n.id); }
    };
    ping();
    _pingIntervals.set(n.id, setInterval(ping, 15000));
  }
}

function timeAgo(iso) {
  if (!iso) return 'never';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

/* ─── Applications ──────────────────────────────────────────────────────── */
const ACTION_ICONS = {
  start:   '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>',
  stop:    '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
  restart: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>',
};

function appSubline(app) {
  if (app.status === 'error') return { text: app.last_error ? String(app.last_error).split('\n')[0] : 'Error', cls: 'error' };
  if (app.no_web) return { text: 'Background worker', cls: '' };
  if (app.app_url) return { text: app.app_url.replace(/^https?:\/\//, ''), cls: '' };
  if (app.domain) return { text: app.domain, cls: '' };
  return { text: (app.repo_url || '').replace(/^https:\/\/github\.com\//, ''), cls: '' };
}

function appRowHTML(app) {
  const reps = app.replicas || [];
  const up = reps.filter(r => r.status === 'running').length;
  const nodeIds = [...new Set(reps.map(r => r.node_id ?? null))];
  const nodeNames = nodeIds.length
    ? nodeIds.map(id => nodeLabel(nodesData.find(n => n.id === id) || (id == null ? null : { name: `node ${id}` }))).join(', ')
    : '—';
  const stats = _appStats.get(app.id);
  const sub = appSubline(app);
  const busy = ['deploying', 'starting', 'stopping', 'restarting'].includes(app.status);
  const isRunning = app.status === 'running';

  return `
    <tr class="clickable" data-app-id="${app.id}">
      <td>
        <div class="app-cell">
          <span class="dot" style="background:${statusColor(app.status)}"></span>
          <span class="app-cell-text">
            <a class="app-cell-name" href="/app?id=${app.id}">${esc(app.name)}</a>
            <span class="app-cell-sub ${sub.cls}">${esc(sub.text)}</span>
          </span>
        </div>
      </td>
      <td class="cell-muted">${esc(appKind(app))}</td>
      <td class="cell-mono">${esc(nodeNames)}</td>
      <td class="num">${reps.length ? `${up}/${reps.length}` : '—'}</td>
      <td class="num">${stats ? `${stats.cpu.toFixed(stats.cpu < 10 ? 1 : 0)}%` : '—'}</td>
      <td class="num">${stats ? fmtMem(stats.mem) : '—'}</td>
      <td>
        <div class="row-actions">
          <button type="button" class="row-action" data-action="${isRunning ? 'stop' : 'start'}" data-perm="apps.${isRunning ? 'stop' : 'start'}" ${busy ? 'disabled' : ''} aria-label="${isRunning ? 'Stop' : 'Start'} ${esc(app.name)}" title="${isRunning ? 'Stop' : 'Start'}">${ACTION_ICONS[isRunning ? 'stop' : 'start']}</button>
          <button type="button" class="row-action" data-action="restart" data-perm="apps.restart" ${busy || !isRunning ? 'disabled' : ''} aria-label="Restart ${esc(app.name)}" title="Restart">${ACTION_ICONS.restart}</button>
        </div>
      </td>
    </tr>`;
}

function renderApps() {
  const wrap = document.getElementById('apps-grid');
  if (!wrap || _noAppsPermission) return;

  if (!appsData.length) {
    wrap.innerHTML = `
      <div class="apps-empty">
        <strong>No applications yet</strong>
        Deploy a repository from GitHub to get started.
        <div style="margin-top:18px"><button class="btn btn-primary" id="empty-deploy-btn" data-perm="apps.deploy">${icon.plus} New app</button></div>
      </div>`;
    document.getElementById('empty-deploy-btn')?.addEventListener('click', () => {
      openDeployModal(app => { window.location.href = `/app?id=${app.id}`; });
    });
    return;
  }

  const rows = appsData.filter(a => _appFilter === 'all' || appCategory(a) === _appFilter);
  wrap.innerHTML = `
    <table class="table apps-table">
      <thead>
        <tr><th>Name</th><th>Type</th><th>Node</th><th class="num">Instances</th><th class="num">CPU</th><th class="num">Memory</th><th><span class="sr-only">Actions</span></th></tr>
      </thead>
      <tbody>${rows.length ? rows.map(appRowHTML).join('') : `<tr><td colspan="7" class="cell-muted" style="padding:28px 0;text-align:center">No ${_appFilter === 'worker' ? 'workers' : _appFilter + ' apps'}</td></tr>`}</tbody>
    </table>`;
  if (window._applyPermVisibility) window._applyPermVisibility(wrap);

  wrap.querySelectorAll('tr[data-app-id]').forEach(tr => {
    const app = appsData.find(a => a.id === parseInt(tr.dataset.appId, 10));
    tr.addEventListener('click', e => {
      const actionBtn = e.target.closest('[data-action]');
      if (actionBtn) { e.stopPropagation(); appAction(app, actionBtn.dataset.action, actionBtn); return; }
      if (e.target.closest('a')) return;
      location.href = `/app?id=${app.id}`;
    });
  });
}

async function appAction(app, action, btn) {
  if (action === 'stop' || action === 'restart') {
    const ok = await confirm(`${action === 'stop' ? 'Stop' : 'Restart'} ${esc(app.name)}?`,
      action === 'stop' ? 'All running instances will be stopped.' : 'All running instances will be restarted.');
    if (!ok) return;
  }
  btn.disabled = true;
  try {
    const fns = { start: api.start, stop: api.stop, restart: api.restart };
    await fns[action](app.id);
    await loadApps();
    toast(`${app.name}: ${action} requested`);
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
  }
}

/* ─── Add Node modal ────────────────────────────────────────────────────── */
let _nodeModalPollTimer = null;
let _nodeModalCountdown = null;

function _clearNodeModalTimers() {
  clearInterval(_nodeModalPollTimer);
  clearInterval(_nodeModalCountdown);
  _nodeModalPollTimer = null;
  _nodeModalCountdown = null;
}

function openAddNodeModal() {
  _clearNodeModalTimers();

  let modal = document.getElementById('add-node-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'add-node-modal';
    modal.className = 'dialog-backdrop';
    modal.innerHTML = `
      <div class="dialog" style="max-width:520px;width:94%">
        <div class="dialog-title" id="node-modal-title">Connect a Node</div>
        <div class="dialog-body" style="padding-top:4px">

          <p style="margin:0 0 18px;font-size:13px;color:var(--text-secondary);line-height:1.6">
            Run this command on your remote server. The agent will automatically register and appear in your dashboard.
          </p>

          <div style="background:var(--bg-elevated);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border)">
              <span style="font-size:11px;font-weight:600;text-transform:uppercase;color:var(--text-muted);letter-spacing:0.06em">Install Command</span>
              <span id="node-countdown" style="font-size:11px;font-family:var(--font-mono);color:var(--yellow)">30:00</span>
            </div>
            <pre id="node-invite-cmd" style="margin:0;padding:14px;font-family:var(--font-mono);font-size:12px;color:var(--text-primary);white-space:pre-wrap;word-break:break-all;line-height:1.6;min-height:52px">Generating…</pre>
            <div style="padding:10px 14px;border-top:1px solid var(--border)">
              <button class="btn btn-secondary btn-sm" id="node-copy-cmd" style="width:100%;justify-content:center;gap:6px">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                Copy Command
              </button>
            </div>
          </div>

          <div id="node-status-waiting" style="display:flex;align-items:center;gap:12px;padding:12px 16px;background:var(--bg-muted);border:1px solid var(--border);border-radius:8px">
            <div class="node-spinner"></div>
            <span style="font-size:13px;color:var(--text-secondary)">Waiting for node to connect…</span>
          </div>
          <div id="node-status-success" style="display:none;align-items:center;gap:12px;padding:12px 16px;background:var(--green-bg);border:1px solid var(--green-border);border-radius:8px">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>
            <span style="font-size:13px;color:var(--green);font-weight:600">Node connected successfully!</span>
          </div>
          <div id="node-create-error" style="display:none;margin-top:12px;font-size:12px;padding:10px 14px;border-radius:8px;background:var(--red-bg);border:1px solid var(--red-border);color:var(--red)"></div>

        </div>
        <div class="dialog-actions">
          <button class="btn btn-secondary" id="node-modal-cancel">Close</button>
          <button class="btn btn-primary" id="node-modal-done" style="display:none">View Node</button>
        </div>
      </div>`;
    document.body.appendChild(modal);

    const closeHandler = () => { _clearNodeModalTimers(); modal.style.display = 'none'; };
    modal.querySelector('#node-modal-cancel').onclick = closeHandler;
    modal.querySelector('#node-modal-done').onclick = () => {
      closeHandler();
      if (modal._connectedId) window.location.href = `/node?id=${modal._connectedId}`;
    };
    modal.querySelector('#node-copy-cmd').onclick = () => {
      const cmd = modal.querySelector('#node-invite-cmd').textContent || '';
      navigator.clipboard.writeText(cmd);
      toast('Command copied');
    };
    modal.addEventListener('click', e => { if (e.target === modal) closeHandler(); });
  }

  modal.style.display = 'flex';
  modal.querySelector('#node-invite-cmd').textContent = 'Generating…';
  modal.querySelector('#node-countdown').textContent = '30:00';
  modal.querySelector('#node-countdown').style.color = 'var(--yellow)';
  modal.querySelector('#node-status-waiting').style.display = 'flex';
  modal.querySelector('#node-status-success').style.display = 'none';
  modal.querySelector('#node-modal-done').style.display = 'none';
  modal.querySelector('#node-modal-cancel').style.display = '';
  modal.querySelector('#node-create-error').style.display = 'none';
  modal.querySelector('#node-modal-title').textContent = 'Connect a Node';
  modal._connectedId = null;

  (async () => {
    try {
      const prevIds = new Set(nodesData.map(n => n.id));
      const invite = await api.createNodeInvite({ note: 'New Node', ttl_minutes: 30 });

      const cmd = `cloudbase connect --main-url ${location.origin} --invite-code ${invite.code} --mode node-only`;
      modal.querySelector('#node-invite-cmd').textContent = cmd;

      const expiresAt = new Date(invite.expires_at);
      const tick = () => {
        const secs = Math.max(0, Math.floor((expiresAt.getTime() - Date.now()) / 1000));
        const m = Math.floor(secs / 60).toString().padStart(2, '0');
        const s = (secs % 60).toString().padStart(2, '0');
        const el = modal.querySelector('#node-countdown');
        if (el) {
          el.textContent = `${m}:${s}`;
          el.style.color = secs < 120 ? 'var(--red)' : 'var(--yellow)';
        }
        if (secs === 0) {
          _clearNodeModalTimers();
          const errEl = modal.querySelector('#node-create-error');
          errEl.textContent = 'Invite expired. Please close and reopen to generate a new one.';
          errEl.style.display = 'block';
        }
      };
      tick();
      _nodeModalCountdown = setInterval(tick, 1000);

      _nodeModalPollTimer = setInterval(async () => {
        try {
          const fresh = await api.listNodes();
          const newNode = fresh.find(n => !prevIds.has(n.id));
          if (newNode) {
            _clearNodeModalTimers();
            modal._connectedId = newNode.id;
            nodesData = fresh;
            renderNodes();
            modal.querySelector('#node-status-waiting').style.display = 'none';
            modal.querySelector('#node-status-success').style.display = 'flex';
            modal.querySelector('#node-modal-done').style.display = '';
            modal.querySelector('#node-modal-cancel').textContent = 'Close';
            modal.querySelector('#node-modal-title').textContent = 'Node Connected';
          }
        } catch {}
      }, 3000);

    } catch (e) {
      modal.querySelector('#node-create-error').textContent = e.message;
      modal.querySelector('#node-create-error').style.display = 'block';
      modal.querySelector('#node-invite-cmd').textContent = 'Failed to generate invite.';
    }
  })();
}

function _waitForCommand(commandId, nodeId) {
  return new Promise((resolve, reject) => {
    const ws = wsNodeEvents(nodeId, event => {
      if (event.type === 'command_update' && event.command_id === commandId) {
        ws.close();
        event.status === 'done' ? resolve(event) : reject(new Error(event.error_message || 'Command failed'));
      }
    });
    // Check current status immediately in case we missed the event
    api.getNodeCommandStatus(nodeId, commandId).then(cmd => {
      if (cmd.status === 'done' || cmd.status === 'failed') {
        ws.close();
        cmd.status === 'done' ? resolve(cmd) : reject(new Error(cmd.error_message || 'Command failed'));
      }
    }).catch(() => {});
    setTimeout(() => { ws.close(); resolve(); }, 60000);
  });
}

