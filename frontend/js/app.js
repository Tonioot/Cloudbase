import { api, wsLogs, wsReplicaLogs, wsStats, wsNodeEvents, PermissionError } from './api.js';
import { auditTableHTML, cssVar, icon, typeIcon, badge, toast, confirm, spinner, fmtUptime, fmtSize, fmtDate, logClass, setBtn, parseDotEnv, pickTextFile, mergeEnvIntoRows } from './utils.js';
import { pickGitHubToken } from './sidebar.js';
import { setCrumbs } from './shell.js';

const params = new URLSearchParams(location.search);
const APP_ID = parseInt(params.get('id'));

let app = null;
let logWs  = null;
let statWs = null;
let logLines = [];
let chartCpu  = null;
let chartMem  = null;
let chartNet  = null;
let chartDisk = null;
let cpuData  = [];
let memData  = [];
let netData  = [];
let diskData = [];
let statsTabActive = false;
let lastStatStatus = null; // 'running' | 'stopped' | null (unknown/loading)
let _settingsInitialized = false;

function _canManageApps() {
  try {
    const perms = JSON.parse(document.body.dataset.permissions || '[]');
    return perms.includes('apps.configure');
  } catch { return false; }
}

window.addEventListener('cloudbase-role-ready', (evt) => {
  if (!_settingsInitialized) return;
  const perms = new Set(evt?.detail?.permissions || []);
  if (!perms.has('apps.configure')) _disableSettingsForViewer();
  else _enableSettingsForEditor();
});

// Show only the controls that apply to this app's type:
// - background workers: no web/nginx features at all
// - static sites: served by nginx on a fixed port; no server process, so no
//   start command (it holds the publish directory), no internal port, and no
//   read-only root (nginx needs to write its cache/pid files)
function _updateAppTypeVisibility(app) {
  const noWeb = !!app.no_web;
  const isStatic = app.app_type === 'static';
  const hide = noWeb ? 'none' : '';
  const el = id => document.getElementById(id);

  // Settings panel
  if (el('cfg-port-field'))          el('cfg-port-field').style.display = (noWeb || isStatic) ? 'none' : '';
  if (el('cfg-docker-readonly-field')) el('cfg-docker-readonly-field').style.display = isStatic ? 'none' : '';
  if (el('cfg-env-static-hint'))     el('cfg-env-static-hint').style.display = isStatic ? '' : 'none';
  if (el('cfg-cmd-label'))           el('cfg-cmd-label').textContent = isStatic ? 'Publish Directory' : 'Start Command';
  if (el('cfg-cmd'))                 el('cfg-cmd').placeholder = isStatic ? 'auto-detect (root, dist, build, …)' : 'npm start';
  // Network section: parent settings-group of cfg-domains-rows
  const networkSection = el('cfg-domains-rows')?.closest('.settings-group');
  if (networkSection)                networkSection.style.display = hide;
  // Maintenance Pages section
  if (el('maintenance-pages-section')) el('maintenance-pages-section').style.display = hide;
  // Actions: nginx config editor is not applicable for no-web apps
  if (el('tile-nginx'))               el('tile-nginx').style.display = hide;

  // Header: the ⋯ menu only holds downtime/update mode, which need nginx
  const moreMenu = el('btn-more-menu')?.closest('.menu-wrap');
  if (moreMenu) moreMenu.style.display = hide;
}

// cfg-no-web checkbox removed — no_web is set at app creation via app type selector

/* ─── Init ──────────────────────────────────────────────────────────────── */
export async function initApp() {
  if (!APP_ID || isNaN(APP_ID)) {
    window.location.href = '/';
    return;
  }

  try {
    app = await api.getApp(APP_ID);
  } catch (err) {
    document.body.innerHTML = `
      <div style="display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:12px;color:var(--muted)">
        <div style="font-size:18px;color:var(--red)">Failed to load application</div>
        <div style="font-size:13px">${err.message}</div>
        <a href="/" style="color:var(--text-2);font-size:13px;margin-top:8px">← Back to dashboard</a>
      </div>`;
    return;
  }

  renderHeader();
  initTabs();
  startBgStats();          // Collect stats in the background from the start
  setInterval(refreshApp, 6000);
}

async function refreshApp() {
  try {
    app = await api.getApp(APP_ID);
    updateHeaderStatus();
    if (statsTabActive) {
      _syncStatsViewFromAppStatus();
    }
  } catch {}
}

function _syncStatsViewFromAppStatus() {
  if (!app) return;
  const stoppedView = document.getElementById('stats-stopped');
  const contentView = document.getElementById('stats-content');
  if (!stoppedView || !contentView) return;

  if (app.status !== 'running') {
    lastStatStatus = 'stopped';
    _removeStatsLoading();
    stoppedView.style.display = 'flex';
    contentView.style.display = 'none';
  }
}

/* ─── Header ────────────────────────────────────────────────────────────── */
function formatPortSummary(app) {
  const replicas = app.replicas || [];
  const running = replicas.filter(r => r.status === 'running');
  const total = replicas.length;
  if (total === 0) {
    return app.port ? `Port ${app.port}` : 'No instances';
  }
  const ports = running.map(r => r.external_port).filter(Boolean);
  const portStr = ports.length ? ports.map(p => `:${p}`).join(' ') : '';
  return `${running.length}/${total} instances running${portStr ? ` (${portStr})` : ''}`;
}

function renderHeader() {
  document.getElementById('app-name').textContent = app.name;
  setCrumbs([{ label: 'Apps', href: '/' }, app.name]);
  document.title = `${app.name} — Cloudbase`;
  document.getElementById('app-meta').textContent = formatPortSummary(app);
  const repoEl = document.getElementById('app-repo-text');
  if (repoEl) repoEl.textContent = (app.repo_url || '').replace(/^https?:\/\/(www\.)?github\.com\//, '');

  // App URL link (custom domain or auto-subdomain)
  const urlLink = document.getElementById('app-url-link');
  const urlText = document.getElementById('app-url-text');
  if (app.app_url && urlLink && urlText) {
    urlLink.href = app.app_url;
    urlText.textContent = app.app_url.replace(/^https?:\/\//, '');
    urlLink.style.display = 'flex';
  }

  const typeIconEl = document.getElementById('app-type-icon');
  if (typeIconEl) typeIconEl.innerHTML = typeIcon[app.app_type] || typeIcon.unknown;

  const typeLabelEl = document.getElementById('app-service-type-badge');
  if (typeLabelEl) {
    typeLabelEl.textContent = app.no_web ? 'Background Worker' : app.app_type === 'static' ? 'Static Site' : 'Web Service';
    typeLabelEl.className = 'service-type-badge' + (app.no_web ? ' service-type-badge--worker' : '');
  }

  updateHeaderStatus();

  document.getElementById('btn-start').addEventListener('click',   () => quickAction('start'));
  document.getElementById('btn-stop').addEventListener('click',    async () => {
    const ok = await confirm('Stop App', `This will stop all running instances of <strong>${app.name}</strong>.`);
    if (ok) quickAction('stop');
  });
  document.getElementById('btn-restart').addEventListener('click', async () => {
    const ok = await confirm('Restart App', `This will restart all running instances of <strong>${app.name}</strong>.`);
    if (ok) quickAction('restart');
  });

  document.getElementById('btn-maintenance-mode').addEventListener('click', () => toggleMode('maintenance'));
  document.getElementById('btn-update-mode').addEventListener('click',      () => toggleMode('update'));
  // Deploy menu: the pull / rebuild flows live on the Settings → Actions tiles
  document.getElementById('menu-pull-rolling')?.addEventListener('click', () => pullAndDeploy('rolling'));
  document.getElementById('menu-pull-bluegreen')?.addEventListener('click', () => pullAndDeploy('blue_green'));
  document.getElementById('menu-pull-rebuild')?.addEventListener('click', () => pullAndDeploy('rebuild'));
  document.getElementById('menu-rebuild')?.addEventListener('click', () => tileAction('rebuild', 'Rebuild'));
  _syncZeroDowntimeButton();
  // Hide web-only controls (downtime/update mode buttons etc.) right away for
  // background workers, not only once the Settings tab has been opened.
  _updateAppTypeVisibility(app);
}

function _syncZeroDowntimeButton() {
  const zdBtn      = document.getElementById('btn-zero-downtime');
  const rollingBtn = document.getElementById('btn-rolling-deploy');

  // Base-domain routed apps can have app_url without app.nginx_enabled.
  const hasPublicRoute = !!(app?.app_url || (app?.nginx_enabled && app?.domain));
  const canDeploy = !!(!app?.no_web && hasPublicRoute);
  document.querySelectorAll('.needs-route').forEach(el => { el.style.display = canDeploy ? '' : 'none'; });

  if (zdBtn) {
    zdBtn.style.display = canDeploy ? '' : 'none';
    if (!zdBtn.dataset.bound) {
      zdBtn.dataset.bound = '1';
      zdBtn.onclick = async () => {
        const ok = await confirm(
          'Blue/Green Deploy',
          'Builds a new image for every instance simultaneously, starts them on new ports, verifies health, then atomically swaps nginx. Old containers stop only after all new ones are live.'
        );
        if (!ok) return;
        zdBtn.disabled = true;
        const orig = zdBtn.innerHTML;
        zdBtn.textContent = 'Deploying…';
        try {
          const res = await api.deployBlueGreen(APP_ID);
          toast(`Blue/Green deploy complete — instance ${res.instance_id}`, 'success');
          app = await api.getApp(APP_ID);
          updateHeaderStatus();
        } catch (e) {
          toast(e.message || 'Blue/Green deploy failed', 'error');
        } finally {
          zdBtn.disabled = false;
          zdBtn.innerHTML = orig;
        }
      };
    }
  }

  if (rollingBtn) {
    rollingBtn.style.display = canDeploy ? '' : 'none';
    if (!rollingBtn.dataset.bound) {
      rollingBtn.dataset.bound = '1';
      rollingBtn.onclick = async () => {
        const ok = await confirm(
          'Rolling Deploy',
          'Replaces each replica one at a time with a freshly built image. While one replica is updating, the others keep serving traffic — no downtime page is shown.'
        );
        if (!ok) return;
        rollingBtn.disabled = true;
        const orig = rollingBtn.innerHTML;
        rollingBtn.textContent = 'Deploying…';
        try {
          const res = await api.deployRolling(APP_ID);
          toast(`Rolling deploy complete — ${res.replaced} replica(s) replaced`, 'success');
          app = await api.getApp(APP_ID);
          updateHeaderStatus();
        } catch (e) {
          toast(e.message || 'Rolling deploy failed', 'error');
        } finally {
          rollingBtn.disabled = false;
          rollingBtn.innerHTML = orig;
        }
      };
    }
  }
}

function _updateHeaderStatus_legacy() {
  document.getElementById('app-badge').innerHTML = badge(app.status);

  const s = app.status;
  const busy = (s === 'deploying');

  const btnStart   = document.getElementById('btn-start');
  const btnStop    = document.getElementById('btn-stop');
  const btnRestart = document.getElementById('btn-restart');

  btnStart.disabled   = (s === 'running') || busy;
  btnStop.disabled    = (s === 'stopped') || busy;
  btnRestart.disabled = busy;

  // Visual: dim the non-applicable button slightly
  btnStart.style.opacity   = (s === 'running') ? '0.4' : '1';
  btnStop.style.opacity    = (s === 'stopped') ? '0.4' : '1';

  // Maintenance / update mode toggle buttons
  const btnMaint  = document.getElementById('btn-maintenance-mode');
  const btnUpdate = document.getElementById('btn-update-mode');
  if (btnMaint && btnUpdate) {
    const canToggle = canToggleMaintenanceMode();
    btnMaint.disabled  = !canToggle;
    btnUpdate.disabled = !canToggle;
    btnMaint.title  = canToggle ? 'Toggle maintenance mode — serves the custom downtime page via nginx'
                                : getMaintenanceToggleDisabledReason();
    btnUpdate.title = canToggle ? 'Toggle update mode — serves the custom update page via nginx'
                                : getMaintenanceToggleDisabledReason();
    btnMaint.classList.toggle('active-maintenance', !!app.maintenance_mode);
    btnUpdate.classList.toggle('active-update',      !!app.update_mode);
  }
}

async function _toggleMode_legacy(type) {
  const btnId = type === 'maintenance' ? 'btn-maintenance-mode' : 'btn-update-mode';
  const btn = document.getElementById(btnId);
  const prev = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${spinner} …`;

  try {
    const fn = type === 'maintenance' ? api.toggleMaintenanceMode : api.toggleUpdateMode;
    app = await fn(APP_ID);
    updateHeaderStatus();
    _updateMaintBadges();
    const isOn = type === 'maintenance' ? app.maintenance_mode : app.update_mode;
    toast(isOn ? `${type === 'maintenance' ? 'Maintenance' : 'Update'} mode enabled`
               : `${type === 'maintenance' ? 'Maintenance' : 'Update'} mode disabled`);
  } catch (e) {
    toast(e.message, 'error');
    try { app = await api.getApp(APP_ID); updateHeaderStatus(); } catch {}
  } finally {
    btn.innerHTML = prev;
    btn.disabled = !app.nginx_enabled;
  }
}

function updateHeaderStatus() {
  const badgeEl = document.getElementById('app-badge');
  // Traffic mode is shown next to the status — it is otherwise only visible in the ⋯ menu
  const modeChip = app.maintenance_mode
    ? ' <span class="pill" style="color:var(--red);border-color:var(--red-border)">Downtime mode</span>'
    : app.update_mode
      ? ' <span class="pill" style="color:var(--yellow);border-color:var(--yellow-border)">Update mode</span>'
      : '';
  badgeEl.innerHTML = badge(app.status) + modeChip;
  document.getElementById('app-meta').textContent = formatPortSummary(app);

  const s = app.status;
  const busy = (s === 'deploying');

  const btnStart   = document.getElementById('btn-start');
  const btnStop    = document.getElementById('btn-stop');
  const btnRestart = document.getElementById('btn-restart');

  btnStart.disabled   = (s === 'running') || busy;
  btnStop.disabled    = (s === 'stopped') || busy;
  btnRestart.disabled = busy;

  const btnMaint  = document.getElementById('btn-maintenance-mode');
  const btnUpdate = document.getElementById('btn-update-mode');
  if (btnMaint && btnUpdate) {
    const canToggle = canToggleMaintenanceMode();
    btnMaint.disabled = !canToggle;
    btnUpdate.disabled = !canToggle;
    btnMaint.title = canToggle
      ? 'Toggle downtime mode - serves the custom downtime page via nginx'
      : getMaintenanceToggleDisabledReason();
    btnUpdate.title = canToggle
      ? 'Toggle update mode - serves the custom update page via nginx'
      : getMaintenanceToggleDisabledReason();
    btnMaint.classList.toggle('active-maintenance', !!app.maintenance_mode);
    btnUpdate.classList.toggle('active-update', !!app.update_mode);
  }

  _syncZeroDowntimeButton();

  refreshMaintenanceUiState();
}

async function toggleMode(type) {
  const btnId = type === 'maintenance' ? 'btn-maintenance-mode' : 'btn-update-mode';
  const btn = document.getElementById(btnId);
  const prev = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `${spinner} ...`;

  try {
    const fn = type === 'maintenance' ? api.toggleMaintenanceMode : api.toggleUpdateMode;
    app = await fn(APP_ID);
    updateHeaderStatus();
    _updateMaintBadges();
    const isOn = type === 'maintenance' ? app.maintenance_mode : app.update_mode;
    toast(isOn ? `${type === 'maintenance' ? 'Downtime' : 'Update'} mode enabled`
               : `${type === 'maintenance' ? 'Downtime' : 'Update'} mode disabled`);
  } catch (e) {
    toast(e.message, 'error');
    try { app = await api.getApp(APP_ID); updateHeaderStatus(); } catch {}
  } finally {
    btn.innerHTML = prev;
    btn.disabled = !canToggleMaintenanceMode();
  }
}

async function quickAction(action) {
  const btn = document.getElementById(`btn-${action}`);
  const prev = btn.innerHTML;
  const transitional = action === 'start' ? 'starting' : action === 'stop' ? 'stopping' : 'restarting';
  const labels = { start: 'Starting…', stop: 'Stopping…', restart: 'Restarting…' };

  // Lock all three buttons and show transitional badge
  ['start','stop','restart'].forEach(a => {
    const b = document.getElementById(`btn-${a}`);
    b.disabled = true;
    b.style.opacity = a === action ? '1' : '0.4';
  });
  btn.innerHTML = `${spinner} ${labels[action]}`;
  document.getElementById('app-badge').innerHTML = badge(transitional);

  // Show action banner in terminal immediately
  if (activeTab === 'logs') _logAction(action, 'begin');

  try {
    const fns = { start: api.start, stop: api.stop, restart: api.restart };
    const result = await fns[action](APP_ID);

    // Remote node: subscribe to events and wait for command completion
    const remoteNodeId = result?.node_id || (app.replicas || []).find(r => r.node_id && !r.node_is_local)?.node_id;
    if (result?.command_id && remoteNodeId) {
      btn.innerHTML = `${spinner} Pending on node…`;
      document.getElementById('app-badge').innerHTML = badge('pending');
      await _waitForRemoteCommand(result.command_id, remoteNodeId);
    } else {
      // Local: clear chart history on start/restart
      if (action === 'start' || action === 'restart') {
        cpuData = [];
        memData = [];
        await new Promise(r => setTimeout(r, 2500));
      }
    }
    app = await api.getApp(APP_ID);
    toast(`${action.charAt(0).toUpperCase() + action.slice(1)} successful`);

    if (activeTab === 'logs') _logAction(action, 'done');
  } catch (e) {
    toast(e.message, 'error');
    if (activeTab === 'logs') _logAction(action, 'fail');
    try { app = await api.getApp(APP_ID); } catch {}
  } finally {
    btn.innerHTML = prev;
    updateHeaderStatus();
    // Keep the current log stream attached so lifecycle output remains visible.
  }
}

function _waitForRemoteCommand(commandId, nodeId) {
  return new Promise((resolve, reject) => {
    let ws = null;
    let resolved = false;

    let interval = null;
    const cleanup = () => {
      resolved = true;
      if (ws) { ws.close(); ws = null; }
      if (interval) { clearInterval(interval); interval = null; }
    };

    const onDone = (cmd) => {
      if (resolved) return;
      cleanup();
      cmd.status === 'done' ? resolve(cmd) : reject(new Error(cmd.error_message || 'Command failed'));
    };

    ws = wsNodeEvents(nodeId, event => {
      if (event.type === 'command_update' && event.command_id === commandId) {
        if (event.status === 'done' || event.status === 'failed') {
          onDone(event);
        }
      }
    });

    // Immediate check + periodic fallback poll
    const check = async () => {
      if (resolved) return;
      try {
        const cmd = await api.getNodeCommandStatus(nodeId, commandId);
        if (cmd.status === 'done' || cmd.status === 'failed') {
          onDone(cmd);
        }
      } catch (e) {}
    };

    check();
    interval = setInterval(check, 3000);

    setTimeout(() => {
      clearInterval(interval);
      if (!resolved) {
        cleanup();
        resolve();
      }
    }, 60000);
  });
}

/* ─── Tabs ──────────────────────────────────────────────────────────────── */
function initTabs() {
  const tabs = ['logs', 'stats', 'files', 'instances', 'settings', 'activity'];
  tabs.forEach(t => {
    document.getElementById(`tab-${t}`).addEventListener('click', () => switchTab(t));
  });
  const saved = sessionStorage.getItem('cloudbase_active_tab');
  switchTab(tabs.includes(saved) ? saved : 'logs');
}

let activeTab = null;

function switchTab(t) {
  if (activeTab === t) return;

  // Deactivate old
  if (activeTab) {
    document.getElementById(`tab-${activeTab}`).classList.remove('active');
    document.getElementById(`panel-${activeTab}`).classList.remove('active');
    teardownTab(activeTab);
  }

  activeTab = t;
  sessionStorage.setItem('cloudbase_active_tab', t);
  document.getElementById(`tab-${t}`).classList.add('active');
  document.getElementById(`panel-${t}`).classList.add('active');
  setupTab(t);
}

function teardownTab(t) {
  if (t === 'logs')  {
    if (logWs) { logWs.close(); logWs = null; }
    clearInterval(_logInstancesTimer); _logInstancesTimer = null;
  }
  if (t === 'stats') { statsTabActive = false; } // Keep statWs alive — data keeps accumulating
  if (t === 'instances' && _instancesRefreshTimer) { clearInterval(_instancesRefreshTimer); _instancesRefreshTimer = null; }
}

function setupTab(t) {
  if (t === 'logs')      initLogs();
  if (t === 'stats')     initStats();
  if (t === 'files')     initFiles();
  if (t === 'instances') initInstances();
  if (t === 'settings')  initSettings();
  if (t === 'activity')  initActivity();
}

/* ─── LOGS ──────────────────────────────────────────────────────────────── */
let _logsInitDone = false;
let _logInstances = new Map();      // id → instance, refreshed while the tab is open
let _logInstancesTimer = null;
let _logSourceKey = null;           // what the terminal currently shows (stream or empty state)

const LOG_LIVE_STATUSES = new Set(['running', 'starting', 'restarting', 'stopping', 'pending', 'deploying']);

function _logOptionLabel(r) {
  const where = `${r.node_name || 'primary'}${r.external_port ? ` :${r.external_port}` : ''}`;
  const state = LOG_LIVE_STATUSES.has(r.status) ? '' : ` · ${String(r.status || '').replace('_', ' ')}`;
  return `#${r.id} · ${where}${state}`;
}

async function _refreshLogInstances() {
  const select = document.getElementById('log-instance-select');
  if (!select) return;
  let instances;
  try { instances = await api.listInstances(APP_ID); } catch { return; }
  _logInstances = new Map(instances.map(r => [String(r.id), r]));

  const current = select.value || 'all';
  select.innerHTML = '<option value="all">All instances</option>';
  instances.forEach(r => {
    const opt = document.createElement('option');
    opt.value = String(r.id);
    opt.textContent = _logOptionLabel(r);
    select.appendChild(opt);
  });
  select.value = _logInstances.has(current) ? current : 'all';

  // Re-render when the selected instance went up or down (e.g. node came back)
  if (_logSourceKey !== _logSourceKeyFor(select.value)) _switchLogInstance();
}

function _logSourceKeyFor(val) {
  if (val === 'all' || val === 'primary') return 'all';
  const r = _logInstances.get(val);
  return r && !LOG_LIVE_STATUSES.has(r.status) ? `empty:${val}:${r.status}` : `live:${val}`;
}

function initLogs() {
  const select = document.getElementById('log-instance-select');

  if (!_logsInitDone) {
    _logsInitDone = true;
    select?.addEventListener('change', () => _switchLogInstance());
  }

  _logSourceKey = null;
  _refreshLogInstances();
  clearInterval(_logInstancesTimer);
  _logInstancesTimer = setInterval(_refreshLogInstances, 5000);
  _switchLogInstance();
}

function _logEmptyState(r) {
  const node = escHtml(r.node_name || 'primary');
  const states = {
    node_offline: ['Node offline', `Instance #${r.id} runs on <strong>${node}</strong>, which can't be reached right now. Logs come back here automatically once the node reconnects.`],
    stopped:      ['Instance stopped', `Instance #${r.id} isn't running, so there is nothing to stream. Start it to see live logs.`],
    error:        ['Instance failed', `Instance #${r.id} isn't running.${r.last_error ? `<code>${escHtml(r.last_error)}</code>` : ''}`],
  };
  const [title, body] = states[r.status] || ['No live logs', `Instance #${r.id} is ${escHtml(String(r.status || 'unknown').replace('_', ' '))}.`];
  return `<div class="log-empty-state"><div class="log-empty-title">${title}</div><p>${body}</p></div>`;
}

// Both sources stream live. "All instances" also carries Cloudbase's own
// messages for this app (image builds, deploy steps, start/stop).
function _switchLogInstance() {
  const select = document.getElementById('log-instance-select');
  const hint   = document.getElementById('log-instance-hint');
  const val    = select?.value || 'all';
  const isAll  = val === 'all' || val === 'primary';
  const terminal = document.getElementById('log-terminal');

  if (logWs) { logWs.close(); logWs = null; }
  _logSourceKey = _logSourceKeyFor(val);
  logLines = [];

  const inst = isAll ? null : _logInstances.get(val);
  if (inst && !LOG_LIVE_STATUSES.has(inst.status)) {
    if (hint) hint.textContent = `Not streaming · instance #${val}`;
    terminal.closest('.logs-panel')?.classList.add('logs-idle');
    terminal.innerHTML = _logEmptyState(inst);
    return;
  }
  terminal.closest('.logs-panel')?.classList.remove('logs-idle');

  if (hint) hint.textContent = isAll ? 'Live · all instances + build & deploy output' : `Live · instance #${val}`;
  const noneLive = isAll && _logInstances.size > 0 && ![..._logInstances.values()].some(r => LOG_LIVE_STATUSES.has(r.status));
  const reset = () => {
    terminal.innerHTML = noneLive
      ? `<div class="log-empty">No instances are running. Build and deploy output still shows up here.</div>`
      : `<div class="log-empty">Waiting for log output…</div>`;
    logLines = [];
  };
  reset();
  logWs = isAll
    ? wsLogs(APP_ID, _appendLogLine, reset)
    : wsReplicaLogs(APP_ID, parseInt(val, 10), _appendLogLine, reset);
}

function _appendLogLine(line) {
  const terminal = document.getElementById('log-terminal');
  if (!terminal) return;
  if (terminal.querySelector('.log-empty')) terminal.innerHTML = '';
  logLines.push(line);

  const div = document.createElement('div');
  div.className = `log-line ${logClass(line)}`;
  div.innerHTML = `<span class="log-num">${String(logLines.length).padStart(4)}</span><span class="log-text">${escHtml(line)}</span>`;
  const atBottom = terminal.scrollHeight - terminal.clientHeight - terminal.scrollTop < 60;
  terminal.appendChild(div);
  if (atBottom) terminal.scrollTop = terminal.scrollHeight;
  if (terminal.childElementCount > 2000) terminal.removeChild(terminal.firstChild);
}

function _logAction(action, phase) {
  const terminal = document.getElementById('log-terminal');
  if (!terminal) return;
  if (terminal.querySelector('.log-empty')) terminal.innerHTML = '';

  const ts = new Date().toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const colors = { start: 'var(--green)', stop: 'var(--red)', restart: 'var(--yellow)' };
  const color  = colors[action] || 'var(--accent)';

  const labels = {
    start:   { begin: '▶  Starting app…',              done: '▶  App started',              fail: '▶  Start failed' },
    stop:    { begin: '■  Stopping app…',               done: '■  App stopped',               fail: '■  Stop failed' },
    restart: { begin: '↺  Restarting app…',             done: '↺  App restarted',             fail: '↺  Restart failed' },
  };
  const text = labels[action]?.[phase] ?? `${action} ${phase}`;

  const sep = document.createElement('div');
  sep.className = 'log-line log-action';
  sep.innerHTML = `<span class="log-num">    </span><span class="log-text" style="color:${color};font-weight:600;letter-spacing:.02em">── ${text} ── ${ts} ──</span>`;
  terminal.appendChild(sep);
  terminal.scrollTop = terminal.scrollHeight;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* ─── STATS — background collection ─────────────────────────────────────── */
function startBgStats() {
  statWs = wsStats(APP_ID, handleStatData);
}

function fmtMb(mb) {
  if (mb === null || mb === undefined) return '—';
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  return `${mb.toFixed(1)} MB`;
}

let _lastInstanceCount = 0;

function _updateStatsContextBar(instanceCount) {
  const bar = document.getElementById('stats-context-bar');
  if (!bar) return;
  if (!instanceCount || instanceCount <= 0) { bar.style.display = 'none'; return; }
  _lastInstanceCount = instanceCount;
  const cpuNote = instanceCount > 1 ? 'CPU avg · memory/network/disk sum' : '';
  bar.innerHTML = `
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="7" width="20" height="14" rx="2"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/></svg>
    <span>${instanceCount} instance${instanceCount !== 1 ? 's' : ''}${instanceCount > 1 ? ' · aggregated' : ''}</span>
    ${cpuNote ? `<span style="color:var(--border);margin:0 2px">·</span><span style="font-style:italic">${cpuNote}</span>` : ''}`;
  bar.style.display = 'flex';
}

function handleStatData(data) {
  if (data.status === 'stopped') {
    lastStatStatus = 'stopped';
    if (statsTabActive) {
      _removeStatsLoading();
      document.getElementById('stats-stopped').style.display = 'flex';
      document.getElementById('stats-content').style.display = 'none';
      const hist = document.getElementById('stats-history-section');
      if (hist) hist.style.display = 'none';
    }
    return;
  }
  // Per-replica frame — not an aggregated app-level frame, skip for charts
  if (data.replica_id != null && data.cpu_percent == null) return;
  lastStatStatus = 'running';

  if (data._instance_count) _updateStatsContextBar(data._instance_count);

  // Always accumulate all four series — even while on a different tab. The
  // server replays its recent frames when the socket opens, so label each
  // point with its own time rather than "now".
  const timestamp = data.timestamp || Date.now();
  const now = new Date(timestamp).toLocaleTimeString('nl', { hour:'2-digit', minute:'2-digit' });
  const netTotal  = (data.net_rx_mb    || 0) + (data.net_tx_mb    || 0);
  const diskTotal = (data.disk_read_mb || 0) + (data.disk_write_mb || 0);
  cpuData.push({ t: now, v: data.cpu_percent || 0, ts: timestamp });
  memData.push({ t: now, v: data.memory_mb   || 0, ts: timestamp });
  netData.push({ t: now, v: netTotal, ts: timestamp });
  diskData.push({ t: now, v: diskTotal, ts: timestamp });
  for (const series of [cpuData, memData, netData, diskData]) {
    if (series.length > 60) series.shift();
  }

  if (!statsTabActive) return;

  _removeStatsLoading();
  document.getElementById('stats-stopped').style.display = 'none';
  const hist = document.getElementById('stats-history-section');
  if (hist) hist.style.display = '';
  document.getElementById('stats-content').style.display = 'block';

  document.getElementById('s-cpu').textContent    = `${(data.cpu_percent || 0).toFixed(1)}%`;
  document.getElementById('s-mem').textContent    = `${(data.memory_mb   || 0).toFixed(0)} MB`;
  document.getElementById('s-uptime').textContent = fmtUptime(data.uptime_seconds || 0);
  document.getElementById('s-syscpu').textContent = `${(data.system_cpu_percent || 0).toFixed(1)}%`;

  // Every app runs in Docker: show container metrics only
  document.getElementById('s-instances').textContent = data._instance_count ?? 1;
  document.getElementById('s-net-rx').textContent = fmtMb(data.net_rx_mb);
  document.getElementById('s-net-tx').textContent = fmtMb(data.net_tx_mb);
  document.getElementById('s-disk-read').textContent  = fmtMb(data.disk_read_mb);
  document.getElementById('s-disk-write').textContent = fmtMb(data.disk_write_mb);

  updateChart(chartCpu,  cpuData);
  updateChart(chartMem,  memData);
  updateChart(chartNet,  netData);
  updateChart(chartDisk, diskData);
}

function initStats() {
  statsTabActive = true;


  initCharts();

  // Do not show indefinite loading when backend already knows the app is not running.
  if (app.status !== 'running') {
    lastStatStatus = 'stopped';
  }

  // Always clear any stale loading spinner before deciding what to show
  _removeStatsLoading();

  const histSection = document.getElementById('stats-history-section');
  if (lastStatStatus === 'stopped') {
    // App is known stopped — show stopped state immediately
    document.getElementById('stats-stopped').style.display = 'flex';
    document.getElementById('stats-content').style.display = 'none';
    if (histSection) histSection.style.display = 'none';
  } else if (cpuData.length > 0) {
    // We have buffered data — show it immediately
    document.getElementById('stats-stopped').style.display = 'none';
    document.getElementById('stats-content').style.display = 'block';
    if (histSection) histSection.style.display = '';
    if (_lastInstanceCount) _updateStatsContextBar(_lastInstanceCount);
    updateChart(chartCpu,  cpuData);
    updateChart(chartMem,  memData);
    updateChart(chartNet,  netData);
    updateChart(chartDisk, diskData);
  } else if (app.status === 'running') {
    // App is running but WebSocket hasn't sent data yet — show content skeleton, not spinner
    document.getElementById('stats-stopped').style.display = 'none';
    document.getElementById('stats-content').style.display = 'block';
    if (histSection) histSection.style.display = '';
  } else {
    // Status unknown — show stopped state rather than an indefinite spinner
    document.getElementById('stats-stopped').style.display = 'flex';
    document.getElementById('stats-content').style.display = 'none';
    if (histSection) histSection.style.display = 'none';
  }

  const historySelect = document.getElementById('history-hours');
  if (historySelect) {
    historySelect.onchange = e => loadStatsHistory(parseInt(e.target.value));
  }
  const exportBtn = document.getElementById('btn-export-stats');
  if (exportBtn && exportBtn.dataset.bound !== '1') {
    exportBtn.dataset.bound = '1';
    exportBtn.onclick = async () => {
      const hours = parseInt(document.getElementById('history-hours')?.value || '24', 10);
      await exportStatsCsv(hours);
    };
  }
  document.querySelectorAll('.history-open-btn').forEach(btn => {
    if (btn.dataset.bound === '1') return;
    btn.dataset.bound = '1';
    btn.onclick = () => openLargeHistoryChart(btn.dataset.historyChart || 'cpu');
  });
  // Defer slightly so canvas has layout dimensions before drawing
  setTimeout(() => loadStatsHistory(24), 100);
}

function _removeStatsLoading() {
  document.getElementById('stats-loading')?.remove();
}

function initCharts() {
  chartCpu  = createChart('chart-cpu',  '--accent', '%',   { maxLabels: 4 });
  chartMem  = createChart('chart-mem',  '--purple', ' MB', { maxLabels: 4 });
  chartNet  = createChart('chart-net',  '--green', ' MB', { maxLabels: 4 });
  chartDisk = createChart('chart-disk', '--yellow', ' MB', { maxLabels: 4 });
}

let chartCpuHistory = null;
let chartMemHistory = null;
let chartNetHistory = null;
let chartDiskHistory = null;
let historySeriesCache = { hours: 24, rows: [], cpuPoints: [], memPoints: [], netPoints: [], diskPoints: [] };

function _fmtHistoryTime(ts, hours) {
  const d = new Date(ts);
  if (hours <= 6) {
    return d.toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit' });
  } else {
    const day  = d.toLocaleDateString('nl', { day: 'numeric', month: 'short' });
    const time = d.toLocaleTimeString('nl', { hour: '2-digit', minute: '2-digit' });
    return `${day} ${time}`;
  }
}

function _sampleHistoryRows(rows, maxPoints = 1200) {
  if (!rows || rows.length <= maxPoints) return rows || [];
  const step = rows.length / maxPoints;
  const sampled = [];
  for (let i = 0; i < maxPoints; i++) {
    const idx = Math.floor(i * step);
    sampled.push(rows[idx]);
  }
  const last = rows[rows.length - 1];
  if (sampled[sampled.length - 1] !== last) sampled[sampled.length - 1] = last;
  return sampled;
}

async function loadStatsHistory(hours) {
  try {
    const data = await api.getStatsHistory(APP_ID, hours);
    if (!data || !data.length) {
      historySeriesCache = { hours, rows: [], cpuPoints: [], memPoints: [], netPoints: [], diskPoints: [] };
      return;
    }
    const sampled = _sampleHistoryRows(data, 1200);
    const cpuPoints  = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.cpu_percent }));
    const memPoints  = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.memory_mb }));
    const netPoints  = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.net_mb || 0 }));
    const diskPoints = sampled.map(r => ({ t: _fmtHistoryTime(r.timestamp, hours), v: r.disk_mb || 0 }));
    historySeriesCache = { hours, rows: data, cpuPoints, memPoints, netPoints, diskPoints };
    if (!chartCpuHistory) chartCpuHistory = createChart('chart-cpu-history', '--accent', '%');
    if (!chartMemHistory) chartMemHistory = createChart('chart-mem-history', '--purple', ' MB');
    if (!chartNetHistory) chartNetHistory = createChart('chart-net-history', '--green', ' MB');
    if (!chartDiskHistory) chartDiskHistory = createChart('chart-disk-history', '--yellow', ' MB');
    updateChart(chartCpuHistory, cpuPoints);
    updateChart(chartMemHistory, memPoints);
    updateChart(chartNetHistory, netPoints);
    updateChart(chartDiskHistory, diskPoints);
  } catch (e) {
    console.warn('Stats history load failed:', e);
  }
}

function _csv(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function exportStatsCsv(hours) {
  try {
    const rows = (historySeriesCache.hours === hours && historySeriesCache.rows?.length)
      ? historySeriesCache.rows
      : await api.getStatsHistory(APP_ID, hours);

    if (!rows || !rows.length) {
      toast('No stats history available in this range', 'error');
      return;
    }

    const csvRows = ['timestamp_utc,cpu_percent,memory_mb,net_mb,disk_mb'];
    rows.forEach(r => {
      csvRows.push([
        _csv(r.timestamp),
        _csv(r.cpu_percent),
        _csv(r.memory_mb),
        _csv(r.net_mb ?? 0),
        _csv(r.disk_mb ?? 0),
      ].join(','));
    });

    const blob = new Blob([csvRows.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `${app.name}-stats-${hours}h-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast('Stats exported as CSV', 'success');
  } catch (e) {
    toast(e.message || 'Failed to export stats', 'error');
  }
}

function openLargeHistoryChart(kind) {
  const map = {
    cpu: { title: 'CPU History', subtitle: 'Averaged per 30s interval', color: '--accent', unit: '%', points: historySeriesCache.cpuPoints },
    memory: { title: 'Memory History', subtitle: 'Averaged per 30s interval', color: '--purple', unit: ' MB', points: historySeriesCache.memPoints },
    network: { title: 'Traffic History', subtitle: 'Cumulative RX+TX per 30s', color: '--green', unit: ' MB', points: historySeriesCache.netPoints },
    disk: { title: 'Disk I/O History', subtitle: 'Cumulative Read+Write per 30s', color: '--yellow', unit: ' MB', points: historySeriesCache.diskPoints },
  };

  const cfg = map[kind] || map.cpu;
  if (!cfg.points || !cfg.points.length) {
    toast('No history data to enlarge yet', 'error');
    return;
  }

  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern" style="max-width:min(1100px,96vw);width:min(1100px,96vw)">
      <div class="dialog-title">${cfg.title}</div>
      <div class="dialog-body" style="padding:14px 16px 10px;display:flex;flex-direction:column;gap:8px">
        <div style="font-size:12px;color:var(--text-muted)">${cfg.subtitle} · Range: last ${historySeriesCache.hours}h</div>
        <div style="height:min(62vh,560px)">
          <canvas id="history-large-canvas" style="width:100%;height:100%;display:block"></canvas>
        </div>
      </div>
      <div class="dialog-actions">
        <button class="btn btn-secondary" id="history-large-close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const canvas = backdrop.querySelector('#history-large-canvas');
  const draw = () => {
    canvas.width = canvas.offsetWidth * devicePixelRatio;
    canvas.height = canvas.offsetHeight * devicePixelRatio;
    drawSparkline(canvas.getContext('2d'), canvas, cfg.points, cfg.color, cfg.unit, { maxLabels: 14 });
  };
  draw();
  window.addEventListener('resize', draw, { passive: true });

  const close = () => {
    window.removeEventListener('resize', draw);
    backdrop.remove();
  };
  backdrop.querySelector('#history-large-close').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
}

function createChart(canvasId, color, unit, opts = {}) {
  const canvas = document.getElementById(canvasId);
  const ctx    = canvas.getContext('2d');

  return {
    canvas, ctx, color, unit, opts,
    draw(data) { drawSparkline(ctx, canvas, data, color, unit, opts); }
  };
}

function updateChart(chart, data) {
  if (!chart) return;
  const canvas = chart.canvas;
  canvas.width  = canvas.offsetWidth  * devicePixelRatio;
  canvas.height = canvas.offsetHeight * devicePixelRatio;
  drawSparkline(chart.ctx, canvas, data, chart.color, chart.unit, chart.opts);
}

function drawSparkline(ctx, canvas, data, color, unit, opts = {}) {
  // Colours may be design tokens ('--accent') so charts follow the theme
  if (color.startsWith('--')) color = cssVar(color);
  const gridColor  = cssVar('--line');
  const labelColor = cssVar('--faint');
  const W = canvas.width, H = canvas.height;
  const dpr = devicePixelRatio;

  ctx.clearRect(0, 0, W, H);
  if (data.length < 2) return;

  const vals  = data.map(d => d.v);
  const rawMax = Math.max(...vals);
  // Nice round ceiling: % → nearest 5, MB → nearest 50
  const yMax = rawMax === 0 ? 10
    : unit === ' MB' ? Math.max(Math.ceil(rawMax / 50) * 50, 50)
    : Math.max(Math.ceil(rawMax / 5) * 5, 5);

  // Dynamic left padding: MB labels are wider
  const pL = unit === ' MB' ? 46 * dpr : 38 * dpr;
  const pR = 10 * dpr;   // right
  const pT = 24 * dpr;   // top   — current-value label
  const pB = 22 * dpr;   // bottom — time labels

  const cW = W - pL - pR;
  const cH = H - pT - pB;
  const xStp = data.length > 1 ? cW / (data.length - 1) : cW;
  const yS   = v => pT + cH - Math.min(Math.max(v, 0) / yMax, 1) * cH;

  // Grid lines + Y-axis labels (0 / 25 / 50 / 75 / 100 % of max)
  ctx.font = `${10 * dpr}px Geist, sans-serif`;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (let i = 0; i <= 4; i++) {
    const frac = i / 4;
    const y    = pT + cH - frac * cH;
    const val  = yMax * frac;

    ctx.strokeStyle = gridColor;
    ctx.lineWidth   = dpr;
    ctx.beginPath(); ctx.moveTo(pL, y); ctx.lineTo(W - pR, y); ctx.stroke();

    const lbl = unit === ' MB'
      ? (val >= 1000 ? (val / 1024).toFixed(1) + 'G' : val.toFixed(0) + 'M')
      : val.toFixed(0) + '%';
    ctx.fillStyle = labelColor;
    ctx.fillText(lbl, pL - 5 * dpr, y);
  }

  // X-axis time labels — calculate max labels that fit without overlap
  const approxCharPx = 6.5 * dpr;
  const sampleLbl = data[0].t || '';
  const lblPx = sampleLbl.length * approxCharPx + 20 * dpr; // label width + generous gap
  const fittingMax = Math.max(2, Math.floor(cW / lblPx));
  const hardMax    = (opts && opts.maxLabels) ? opts.maxLabels : fittingMax;
  const labelCount = Math.min(hardMax, fittingMax, data.length);
  const step = Math.max(1, Math.floor((data.length - 1) / (labelCount - 1)));
  ctx.fillStyle = labelColor;
  ctx.font = `${9.5 * dpr}px Geist, sans-serif`;
  ctx.textBaseline = 'top';
  const drawnX = new Set();
  for (let i = 0; i < data.length; i += step) {
    if (!data[i].t) continue;
    const x = pL + i * xStp;
    ctx.textAlign = i === 0 ? 'left' : 'center';
    ctx.fillText(data[i].t, x, pT + cH + 5 * dpr);
    drawnX.add(i);
  }
  // Ensure the last point's time is shown, but only if it won't overlap the previous label
  const last = data.length - 1;
  if (!drawnX.has(last) && data[last].t) {
    const lastX = pL + last * xStp;
    const prevIdx = drawnX.size > 0 ? Math.max(...drawnX) : 0;
    const prevX   = pL + prevIdx * xStp;
    if (lastX - prevX > lblPx) {
      ctx.textAlign = 'right';
      ctx.fillText(data[last].t, lastX, pT + cH + 5 * dpr);
    }
  }

  // Gradient fill
  const grad = ctx.createLinearGradient(0, pT, 0, pT + cH);
  grad.addColorStop(0, color + '1c');
  grad.addColorStop(1, color + '00');
  ctx.beginPath();
  ctx.moveTo(pL, yS(vals[0]));
  for (let i = 1; i < vals.length; i++) ctx.lineTo(pL + i * xStp, yS(vals[i]));
  ctx.lineTo(pL + (vals.length - 1) * xStp, pT + cH);
  ctx.lineTo(pL, pT + cH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Line
  ctx.beginPath();
  ctx.moveTo(pL, yS(vals[0]));
  for (let i = 1; i < vals.length; i++) ctx.lineTo(pL + i * xStp, yS(vals[i]));
  ctx.strokeStyle = color;
  ctx.lineWidth   = 1.5 * dpr;
  ctx.lineJoin    = 'round';
  ctx.lineCap     = 'round';
  ctx.stroke();

  // Current value — top right, coloured
  const cur = vals[vals.length - 1];
  const curLbl = unit === ' MB'
    ? (cur >= 100 ? `${cur.toFixed(0)} MB` : `${cur.toFixed(1)} MB`)
    : `${cur.toFixed(1)}%`;
  ctx.font         = `600 ${12 * dpr}px Geist, sans-serif`;
  ctx.textAlign    = 'right';
  ctx.textBaseline = 'top';
  ctx.fillStyle    = color;
  ctx.fillText(curLbl, W - pR, 4 * dpr);
}

/* ─── FILES ─────────────────────────────────────────────────────────────── */
let currentFilePath = '';

async function initFiles() {
  await loadDir('');
}

async function loadDir(path) {
  currentFilePath = path;
  const list = document.getElementById('files-list');
  const bcrumb = document.getElementById('files-breadcrumb');
  const isRemote = (app.replicas || []).some(r => r.node_id && !r.node_is_local);
  list.innerHTML = `<div style="padding:16px;color:var(--text-muted);font-size:12px">${isRemote && path === '' ? 'Fetching from node…' : 'Loading…'}</div>`;

  try {
    const data = await api.listFiles(APP_ID, path);

    // Breadcrumb
    const parts = data.path === '.' ? [] : data.path.split('/').filter(Boolean);
    bcrumb.innerHTML = renderBreadcrumb(parts);
    bcrumb.querySelectorAll('.crumb-btn').forEach(btn => {
      btn.addEventListener('click', () => loadDir(btn.dataset.path));
    });

    // Entries
    list.innerHTML = '';

    if (path !== '' && path !== '.') {
      const up = document.createElement('div');
      up.className = 'file-entry';
      up.innerHTML = `${icon.folder} ..`;
      up.addEventListener('click', () => loadDir(parts.slice(0,-1).join('/')));
      list.appendChild(up);
    }

    if (!data.entries || data.entries.length === 0) {
      const empty = document.createElement('div');
      empty.style.padding = '32px 16px';
      empty.style.textAlign = 'center';
      empty.style.color = 'var(--text-muted)';
      empty.style.fontSize = '12px';
      empty.innerHTML = `<div style="margin-bottom:8px;opacity:0.3">${icon.server}</div>This directory is empty`;
      list.appendChild(empty);
    } else {
      data.entries.forEach(entry => {
        const el = document.createElement('div');
        el.className = 'file-entry';
        el.innerHTML = entry.is_dir
          ? `<span class="dir-icon">${icon.folder}</span><span>${entry.name}</span>`
          : `<span class="file-icon">${icon.file}</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${entry.name}</span><span class="file-size">${fmtSize(entry.size)}</span>`;

        el.addEventListener('click', () => {
          if (entry.is_dir) loadDir(entry.path);
          else openFile(entry, el);
        });
        list.appendChild(el);
      });
    }

  } catch (e) {
    console.error('[Files] Load failed:', e);
    list.innerHTML = `<div style="padding:16px;color:var(--red);font-size:12px">
      <strong>Error loading files:</strong><br>${e.message}
    </div>`;
  }
}

function renderBreadcrumb(parts) {
  const items = [{ label:'~', path:'' }, ...parts.map((p, i) => ({ label:p, path:parts.slice(0,i+1).join('/') }))];
  return items.map((item, i) => `
    ${i > 0 ? '<span class="sep">/</span>' : ''}
    <button class="crumb-btn" data-path="${item.path}">${item.label}</button>
  `).join('');
}

async function openFile(entry, el) {
  document.querySelectorAll('.file-entry.active').forEach(e => e.classList.remove('active'));
  el.classList.add('active');

  const header  = document.getElementById('file-viewer-header');
  const hint    = document.getElementById('file-empty-hint');
  const content = document.getElementById('file-content');

  header.innerHTML = `
    ${icon.file}
    <span class="file-path">${entry.path}</span>
    <span class="file-mime">Loading…</span>`;

  hint.style.display    = 'none';
  content.style.display = 'block';
  content.textContent   = '';

  try {
    const data = await api.fileContent(APP_ID, entry.path);
    header.querySelector('.file-mime').textContent = data.mime || '';

    if (data.binary) {
      content.textContent = '[Binary file — cannot display]';
    } else {
      content.textContent = data.content || '';
    }
  } catch (e) {
    content.textContent = `Error: ${e.message}`;
  }
}

/* ─── SETTINGS ──────────────────────────────────────────────────────────── */function showCertPicker(inputEl, items, label, displayEl) {
  document.querySelectorAll('.cert-picker').forEach(p => p.remove());
  if (!items.length) { toast(`No ${label} found in app folder`, 'warn'); return; }

  const picker = document.createElement('div');
  picker.className = 'cert-picker';
  picker.style.cssText = 'position:absolute;z-index:9999;background:var(--pop);border:1px solid var(--line-strong);border-radius:8px;max-height:200px;overflow-y:auto;box-shadow:var(--shadow-lg);font-size:12px;padding:4px;';

  items.forEach(path => {
    const row = document.createElement('div');
    row.textContent = path;
    row.style.cssText = 'padding:7px 10px;border-radius:6px;cursor:pointer;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
    row.addEventListener('mouseenter', () => row.style.background = 'var(--hover)');
    row.addEventListener('mouseleave', () => row.style.background = '');
    row.addEventListener('click', () => {
      inputEl.value = path;
      if (displayEl) { displayEl.textContent = path.split('/').pop(); displayEl.classList.add('has-value'); }
      picker.remove();
    });
    picker.appendChild(row);
  });

  // Anchor to the visible row container (cert-upload-row), not the hidden input
  const anchorEl = displayEl ? displayEl.closest('.cert-upload-row') || displayEl : inputEl;
  const rect = anchorEl.getBoundingClientRect();
  picker.style.top   = `${rect.bottom + window.scrollY + 4}px`;
  picker.style.left  = `${rect.left + window.scrollX}px`;
  picker.style.width = `${Math.max(rect.width, 280)}px`;
  document.body.appendChild(picker);

  const close = e => { if (!picker.contains(e.target)) { picker.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}
async function initActivity() {
  const wrap = document.getElementById('audit-log-table-wrap');
  try {
    const entries = await api.getAuditLog(APP_ID, 100);
    if (!entries || !entries.length) {
      wrap.innerHTML = '<div class="apps-empty">No activity recorded yet.</div>';
      return;
    }
    wrap.innerHTML = auditTableHTML(entries);
  } catch (e) {
    wrap.innerHTML = `<div style="color:var(--red);padding:20px;font-size:13px">${e.message}</div>`;
  }
}

function initSettings() {
  const isViewer = !_canManageApps();
  _settingsInitialized = true;

  // Info rows
  document.getElementById('si-name').textContent  = app.name;
  document.getElementById('si-repo').textContent  = app.repo_url;
  document.getElementById('si-type').textContent  = app.app_type || '—';
  document.getElementById('si-date').textContent  = fmtDate(app.created_at);

  // Form fields
  document.getElementById('cfg-cmd').value          = app.start_command  || '';
  document.getElementById('cfg-build').value        = app.build_command  || '';
  document.getElementById('cfg-port').value         = app.port           || '';
  // Domains list (primary first, then extras)
  const domainsContainer = document.getElementById('cfg-domains-rows');
  domainsContainer.innerHTML = '';
  const allDomains = [app.domain, ...(app.extra_domains || [])].filter(Boolean);
  if (allDomains.length === 0) addDomainRow(domainsContainer, '');
  else allDomains.forEach(d => addDomainRow(domainsContainer, d));
  document.getElementById('cfg-add-domain').onclick = () => addDomainRow(domainsContainer, '');

  // Redirect domains
  const redirectContainer = document.getElementById('cfg-redirect-domains-rows');
  redirectContainer.innerHTML = '';
  (app.redirect_domains || []).forEach(d => addDomainRow(redirectContainer, d));
  document.getElementById('cfg-add-redirect-domain').onclick = () => addDomainRow(redirectContainer, '');

  _updateAppTypeVisibility(app);
  document.getElementById('cfg-autostart').checked  = !!app.auto_start;
  document.getElementById('cfg-restart-policy').value = app.restart_policy || 'no';
  document.getElementById('cfg-docker-cpu').value = app.docker_cpu_limit || '';
  document.getElementById('cfg-docker-memory').value = app.docker_memory_limit_mb || '';
  document.getElementById('cfg-docker-readonly').checked = !!app.docker_read_only_root;
  document.getElementById('cfg-docker-tmpfs-enabled').checked = !!app.docker_tmpfs_enabled;
  document.getElementById('cfg-docker-tmpfs-size').value = app.docker_tmpfs_size_mb || '';
  const dockerSection = document.getElementById('docker-runtime-section');

  // Autoscaling
  const autoscaleEnabled = !!app.autoscale_enabled;
  document.getElementById('cfg-autoscale-enabled').checked = autoscaleEnabled;
  document.getElementById('cfg-autoscale-min').value = app.autoscale_min_replicas || 1;
  document.getElementById('cfg-autoscale-max').value = app.autoscale_max_replicas || 4;
  document.getElementById('cfg-autoscale-cpu').value = app.autoscale_cpu_target || 70;
  document.getElementById('autoscale-options').style.display = autoscaleEnabled ? '' : 'none';
  document.getElementById('cfg-autoscale-enabled').onchange = function() {
    document.getElementById('autoscale-options').style.display = this.checked ? '' : 'none';
  };
  if (dockerSection) dockerSection.style.display = '';

  // Cert/key hidden inputs + filename display
  function setCertDisplay(inputId, nameId, path) {
    document.getElementById(inputId).value = path || '';
    const nameEl = document.getElementById(nameId);
    if (path) { nameEl.textContent = path.split('/').pop(); nameEl.classList.add('has-value'); }
    else      { nameEl.textContent = 'No file selected'; nameEl.classList.remove('has-value'); }
  }
  setCertDisplay('cfg-cert', 'cfg-cert-name', app.ssl_cert_path || '');
  setCertDisplay('cfg-key',  'cfg-key-name',  app.ssl_key_path  || '');

  // Env vars — values are write-only: server returns names with empty values.
  // Rows with an empty value field mean "keep existing value unless user types a new one".
  const envContainer = document.getElementById('cfg-env-rows');
  envContainer.innerHTML = '';
  Object.keys(app.env_vars || {}).forEach(k => addEnvRow(envContainer, k, ''));

  const updateEnvCount = () => {
    const n = envContainer.querySelectorAll('.env-row').length;
    document.getElementById('cfg-env-count').textContent = n ? `${n} variable${n === 1 ? '' : 's'}` : '';
  };
  updateEnvCount();
  envContainer.onclick = () => setTimeout(updateEnvCount);  // after row removal

  document.getElementById('cfg-add-env').onclick = () => {
    const row = addEnvRow(envContainer, '', '');
    row.scrollIntoView({ block: 'nearest' });
    row.querySelector('[data-env-key]').focus();
    updateEnvCount();
  };

  document.getElementById('cfg-import-env').onclick = async () => {
    const text = await pickTextFile('.env,.txt,text/plain');
    if (text == null) return;
    const vars = parseDotEnv(text);
    if (!Object.keys(vars).length) { toast('No variables found in that file', 'warn'); return; }
    const { added, updated } = mergeEnvIntoRows(envContainer, vars, (k, v) => addEnvRow(envContainer, k, v));
    updateEnvCount();
    toast(`Imported ${added + updated} variable(s): ${added} new, ${updated} updated. Click Save to apply.`, 'success');
  };

  // Show current token hint (label for vault tokens, '****' for inline)
  const tokenCurrentEl = document.getElementById('cfg-token-current');
  const tokenCurrentLabel = document.getElementById('cfg-token-current-label');
  if (tokenCurrentEl && tokenCurrentLabel) {
    if (app.github_token_label) {
      tokenCurrentLabel.textContent = app.github_token_label;
      tokenCurrentEl.style.display = '';
    } else {
      tokenCurrentEl.style.display = 'none';
    }
  }
  // Pre-fill vault token ID so save keeps it unless user changes it
  const tokenIdEl = document.getElementById('cfg-token-id');
  if (tokenIdEl) tokenIdEl.value = app.github_token_id || '';

  // Pick saved GitHub token
  document.getElementById('cfg-token-pick').onclick = () => {
    pickGitHubToken(document.getElementById('cfg-token'), document.getElementById('cfg-token-id'));
  };

  // Save
  document.getElementById('btn-save').onclick = saveSettings;

  // DNS Setup modal (from Settings header)
  const dnsBtn = document.getElementById('btn-dns-setup');
  const dnsModal = document.getElementById('dns-setup-modal');
  const dnsModalClose = document.getElementById('dns-modal-close');
  if (dnsBtn && dnsModal) {
    dnsBtn.onclick = async () => {
      const ipEl = document.getElementById('dns-server-ip');
      const domainEl = document.getElementById('dns-app-domain');

      if (domainEl) {
        const domain = app && app.domain ? app.domain : '(no domain configured)';
        domainEl.textContent = domain;
        domainEl.style.color = app && app.domain ? 'var(--text-primary)' : 'var(--text-muted)';
      }

      let serverIp = null;
      if (ipEl) {
        ipEl.textContent = 'Loading…';
        ipEl.style.cursor = 'default';
        ipEl.onclick = null;
      }

      try {
        const nodes = await api.listNodes();
        const primaryNode = nodes.find(n => n.is_local) || nodes.find(n => n.role === 'main') || nodes[0] || null;
        if (primaryNode) {
          const meta = primaryNode.metadata || {};
          const parsedHost = primaryNode.api_base_url ? (() => {
            try { return new URL(primaryNode.api_base_url).hostname; } catch { return null; }
          })() : null;
          const isPublicIpAddress = value => {
            if (!value || typeof value !== 'string') return false;
            const v = value.trim();

            if (/^(\d{1,3}\.){3}\d{1,3}$/.test(v)) {
              const parts = v.split('.').map(Number);
              if (parts.some(n => Number.isNaN(n) || n < 0 || n > 255)) return false;
              if (parts[0] === 10) return false;
              if (parts[0] === 127) return false;
              if (parts[0] === 0) return false;
              if (parts[0] === 169 && parts[1] === 254) return false;
              if (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) return false;
              if (parts[0] === 192 && parts[1] === 168) return false;
              if (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127) return false;
              return true;
            }

            if (/^[0-9a-fA-F:]+$/.test(v) && v.includes(':')) {
              const low = v.toLowerCase();
              if (low === '::1') return false;
              if (low.startsWith('fe80:')) return false;
              if (low.startsWith('fc') || low.startsWith('fd')) return false;
              return true;
            }

            return false;
          };

          const candidates = [meta.public_ip, primaryNode.public_host, parsedHost];
          serverIp = candidates.find(isPublicIpAddress) || null;
        }
      } catch {
        // Keep fallback text below
      }

      if (ipEl) {
        if (serverIp) {
          ipEl.textContent = serverIp;
          ipEl.style.cursor = 'pointer';
          ipEl.onclick = () => {
            navigator.clipboard.writeText(serverIp).then(() => toast('IP copied', 'success')).catch(() => {});
          };
        } else {
          ipEl.textContent = 'IP not available';
          ipEl.style.cursor = 'default';
        }
      }

      dnsModal.style.display = 'flex';
    };
    if (dnsModalClose) dnsModalClose.onclick = () => { dnsModal.style.display = 'none'; };
    dnsModal.onclick = e => { if (e.target === dnsModal) dnsModal.style.display = 'none'; };
  }

  // Action tiles
  document.getElementById('tile-pull').onclick = () => tileAction('pull', 'Pull');
  document.getElementById('tile-nginx').onclick = openNginxModal;

  const pullTitle = document.getElementById('tile-pull-title');
  const pullSub = document.getElementById('tile-pull-sub');
  if (pullTitle) pullTitle.textContent = 'Pull + Rebuild';
  if (pullSub) pullSub.textContent = 'Pick a commit, sync code, and rebuild without stop/restart';

  // Cert scan buttons (search within app folder only)
  document.getElementById('cfg-scan-cert').onclick = async () => {
    const btn = document.getElementById('cfg-scan-cert');
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const { certs } = await api.discoverAppCerts(APP_ID);
      showCertPicker(document.getElementById('cfg-cert'), certs, 'certificates', document.getElementById('cfg-cert-name'));
    } catch { toast('Scan failed', 'error'); }
    finally { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Scan'; }
  };
  document.getElementById('cfg-scan-key').onclick = async () => {
    const btn = document.getElementById('cfg-scan-key');
    btn.disabled = true; btn.textContent = 'Scanning…';
    try {
      const { keys } = await api.discoverAppCerts(APP_ID);
      showCertPicker(document.getElementById('cfg-key'), keys, 'private keys', document.getElementById('cfg-key-name'));
    } catch { toast('Scan failed', 'error'); }
    finally { btn.disabled = false; btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Scan'; }
  };

  // Cert upload buttons
  document.getElementById('cfg-upload-cert').onclick = () => document.getElementById('cfg-cert-file').click();
  document.getElementById('cfg-cert-file').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    document.getElementById('cfg-upload-cert').disabled = true;
    try {
      const res = await api.uploadAppCert(APP_ID, file);
      setCertDisplay('cfg-cert', 'cfg-cert-name', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { document.getElementById('cfg-upload-cert').disabled = false; e.target.value = ''; }
  };
  document.getElementById('cfg-upload-key').onclick = () => document.getElementById('cfg-key-file').click();
  document.getElementById('cfg-key-file').onchange = async e => {
    const file = e.target.files[0]; if (!file) return;
    document.getElementById('cfg-upload-key').disabled = true;
    try {
      const res = await api.uploadAppCert(APP_ID, file);
      setCertDisplay('cfg-key', 'cfg-key-name', res.path);
    } catch (err) { toast(err.message, 'error'); }
    finally { document.getElementById('cfg-upload-key').disabled = false; e.target.value = ''; }
  };

  // Delete
  document.getElementById('btn-delete').onclick = async () => {
    const ok = await confirm('Delete Application', `This will permanently remove "${app.name}" and all its files. This action cannot be undone.`);
    if (!ok) return;
    try {
      await api.deleteApp(APP_ID);
      window.location.href = '/';
    } catch (e) {
      toast(e.message, 'error');
    }
  };

  // Maintenance pages section
  initMaintenanceSettings();
  _initMaintModal();

  if (isViewer) _disableSettingsForViewer();
  else _enableSettingsForEditor();
}

function _disableSettingsForViewer() {
  const panel = document.getElementById('panel-settings');
  if (!panel) return;
  if (panel.dataset.viewerLocked === '1') return;

  // Disable all inputs, selects, textareas, buttons
  panel.querySelectorAll('input, select, textarea, button').forEach(el => {
    el.disabled = true;
  });

  // Add a notice banner inside the settings bar
  const bar = panel.querySelector('.settings-bar');
  if (bar && !bar.querySelector('.viewer-notice')) {
    const notice = document.createElement('span');
    notice.className = 'viewer-notice';
    notice.style.cssText = 'font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:5px';
    notice.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg> Read-only`;
    bar.appendChild(notice);
  }
  panel.dataset.viewerLocked = '1';
}

function _enableSettingsForEditor() {
  const panel = document.getElementById('panel-settings');
  if (!panel) return;
  if (panel.dataset.viewerLocked !== '1') return;

  panel.querySelectorAll('input, select, textarea, button').forEach(el => {
    el.disabled = false;
  });
  panel.querySelector('.viewer-notice')?.remove();
  delete panel.dataset.viewerLocked;
}

/* ─── Visitor pages (maintenance pages) ─────────────────────────────────── */

const VP_PAGES = ['downtime', 'update', 'restart', 'starting'];

// Defaults the server uses for empty fields (nginx_manager.PAGE_DEFAULTS)
const MAINT_PAGE_DEFAULTS = {
  downtime: { title: "Down for Maintenance", message: "We'll be back shortly.", color: '#e5484d' },
  update: { title: "Updating…", message: "We’re deploying a new version. Check back soon.", color: '#f5a524' },
  restart: { title: "Restarting…", message: "The server is restarting. This only takes a moment.", color: '#3b82f6' },
  starting: { title: "Starting…", message: "The service is starting up. This only takes a moment.", color: '#3b82f6' },
};

const VP_WHEN = {
  downtime: 'Shown when <strong>downtime mode</strong> is on, and automatically when no instance answers (crash, stop, 502/503).',
  update:   'Shown while <strong>update mode</strong> is on — for planned maintenance and longer deploys.',
  restart:  'Shown automatically while the app restarts, until it answers again.',
  starting: 'Shown automatically while the app starts for the first time, until it answers.',
};

// Branding & style fields that “Apply to all pages” copies
const VP_SHARED_KEYS = ['logo_data', 'brand_name', 'color', 'theme', 'background'];

let _vp = null; // { page, drafts, saved, previewTimer, device, scheme }

// Maintenance pages are served by Cloudbase's nginx on the app's domain, so
// they need one: a custom domain, or the automatic subdomain from the base
// domain (app_url without app.domain). Mirrors _has_public_nginx_domain()
// in the backend.
function canToggleMaintenanceMode() {
  if (app.no_web) return false;
  const customDomain = !!(app.nginx_enabled && app.domain);
  const baseDomainRoute = !!(app.app_url && !app.domain);
  return customDomain || baseDomainRoute;
}

function getMaintenanceToggleDisabledReason() {
  if (app.domain && !app.nginx_enabled) return 'The nginx config for this domain is not active (writing it may have failed) — save the Network settings to retry';
  return 'Requires a domain: add one under Network, or set a base domain in system settings';
}

function refreshMaintenanceUiState() {
  const canServeMaintenance = canToggleMaintenanceMode();
  const noNginxWarn = document.getElementById('maint-no-nginx-warn');
  if (noNginxWarn) noNginxWarn.style.display = canServeMaintenance ? 'none' : '';
  const noNginxText = document.getElementById('maint-no-nginx-text');
  if (noNginxText && !canServeMaintenance) noNginxText.textContent = getMaintenanceToggleDisabledReason();

  ['downtime', 'update', 'restart', 'starting'].forEach(type => {
    const btn = document.getElementById(`btn-open-${type}-modal`);
    if (!btn) return;
    btn.disabled = false;
    btn.title = canServeMaintenance
      ? `Edit the ${type} page`
      : 'You can edit these pages now; they will be served once nginx/domain routing is configured';
  });
}

function initMaintenanceSettings() {
  _updateMaintBadges();
  refreshMaintenanceUiState();
  VP_PAGES.forEach(type => {
    document.getElementById(`btn-open-${type}-modal`)?.addEventListener('click', () => openMaintModal(type));
  });
}

function _vpPageFromApp(type) {
  const c = app[`${type}_page`] || {};
  return {
    title: c.title || '',
    message: c.message || '',
    color: c.color || MAINT_PAGE_DEFAULTS[type].color,
    status_url: c.status_url || '',
    custom_html: c.custom_html || '',
    logo_data: c.logo_data || null,
    theme: c.theme || 'auto',
    background: c.background || 'none',
    brand_name: c.brand_name || '',
  };
}

function _vpPayloadPage(d) {
  return {
    title: d.title.trim() || null,
    message: d.message.trim() || null,
    color: /^#[0-9a-fA-F]{6}$/.test(d.color) ? d.color : null,
    status_url: d.status_url.trim() || null,
    custom_html: d.custom_html.trim() ? d.custom_html : null,
    logo_data: d.logo_data || null,
    theme: d.theme,
    background: d.background,
    brand_name: d.brand_name.trim() || null,
  };
}

function openMaintModal(type = 'downtime') {
  const drafts = Object.fromEntries(VP_PAGES.map(t => [t, _vpPageFromApp(t)]));
  _vp = {
    page: type,
    drafts,
    saved: JSON.stringify(drafts),
    previewTimer: null,
    device: _vp?.device || 'desktop',
    scheme: _vp?.scheme || (document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light'),
  };
  document.getElementById('vp-url').textContent = (app.app_url || (app.domain ? `https://${app.domain}` : `${app.name}.example.com`)).replace(/^https?:\/\//, '');
  document.getElementById('vp-backdrop').style.display = 'flex';
  _vpSetDevice(_vp.device);
  _vpSetScheme(_vp.scheme);
  _vpShowPage(type);
  document.getElementById('vp-f-title').focus();
}

function _vpShowPage(type) {
  _vp.page = type;
  const d = _vp.drafts[type];
  const defaults = MAINT_PAGE_DEFAULTS[type];

  document.querySelectorAll('.vp-tabs [data-page]').forEach(b => {
    const on = b.dataset.page === type;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', String(on));
  });
  document.getElementById('vp-when').innerHTML = VP_WHEN[type];

  const title = document.getElementById('vp-f-title');
  title.value = d.title;
  title.placeholder = defaults.title;
  const msg = document.getElementById('vp-f-message');
  msg.value = d.message;
  msg.placeholder = defaults.message;
  document.getElementById('vp-f-status').value = d.status_url;
  document.getElementById('vp-f-brand').value = d.brand_name;
  _vpSetColor(d.color, false);
  document.querySelectorAll('.vp-seg').forEach(seg => _vpSetSeg(seg, d[seg.dataset.key]));
  _vpRenderLogo();

  const customOn = !!d.custom_html;
  document.getElementById('vp-custom-on').checked = customOn;
  document.getElementById('vp-f-custom').value = d.custom_html;
  document.getElementById('vp-advanced').open = customOn;
  _vpSyncCustom();

  _vpUpdateDirty();
  _vpSyncScheme();
  _vpPreview(true);
}

function _vpSetSeg(seg, value) {
  seg.querySelectorAll('button').forEach(b => {
    const on = b.dataset.value === value;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', String(on));
  });
}

function _vpSetColor(color, store = true) {
  const valid = /^#[0-9a-fA-F]{6}$/.test(color);
  document.getElementById('vp-f-color').value = color;
  if (valid) document.getElementById('vp-color-picker').value = color;
  document.querySelectorAll('#vp-swatches [data-color]').forEach(b => b.classList.toggle('active', b.dataset.color.toLowerCase() === color.toLowerCase()));
  if (store) {
    _vp.drafts[_vp.page].color = color;
    _vpChanged();
  }
}

function _vpRenderLogo() {
  const data = _vp.drafts[_vp.page].logo_data;
  const thumb = document.getElementById('vp-logo-thumb');
  thumb.innerHTML = data ? `<img src="${escAttr(data)}" alt="Logo">` : '<span>No logo</span>';
  thumb.classList.toggle('has-logo', !!data);
  document.getElementById('vp-logo-remove').hidden = !data;
  document.getElementById('vp-logo-upload').textContent = data ? 'Replace' : 'Upload';
}

function _vpSyncCustom() {
  const on = document.getElementById('vp-custom-on').checked;
  document.getElementById('vp-f-custom').hidden = !on;
  // Custom HTML replaces the template, so the other fields don't apply
  ['vp-fields', 'vp-brand', 'vp-style'].forEach(id => document.getElementById(id).classList.toggle('vp-muted', on));
}

function _vpChanged() {
  _vpUpdateDirty();
  _vpSyncScheme();
  _vpPreview();
}

// The light/dark preview switch only matters when the page follows the visitor
function _vpSyncScheme() {
  document.getElementById('vp-scheme').hidden = _vp.drafts[_vp.page].theme !== 'auto';
}

function _vpUpdateDirty() {
  const dirty = JSON.stringify(_vp.drafts) !== _vp.saved;
  const el = document.getElementById('vp-dirty');
  el.textContent = dirty ? 'Unsaved changes' : '';
  document.getElementById('vp-save').disabled = !dirty;
  // Mark tabs whose page differs from what is saved
  const saved = JSON.parse(_vp.saved);
  document.querySelectorAll('.vp-tabs [data-page]').forEach(b => {
    b.classList.toggle('edited', JSON.stringify(_vp.drafts[b.dataset.page]) !== JSON.stringify(saved[b.dataset.page]));
  });
}

function _vpPreview(now = false) {
  clearTimeout(_vp.previewTimer);
  _vp.previewTimer = setTimeout(async () => {
    const page = _vp.page;
    const body = _vpPayloadPage(_vp.drafts[page]);
    // "Automatic" follows the visitor's system; preview it in the chosen scheme
    if (body.theme === 'auto') body.theme = _vp.scheme;
    try {
      const html = await api.renderMaintenancePage(APP_ID, page, body);
      if (_vp.page === page) document.getElementById('vp-frame').srcdoc = html;
    } catch { /* keep the last preview */ }
  }, now ? 0 : 250);
}

function _vpSetDevice(device) {
  _vp.device = device;
  document.querySelectorAll('#vp-device button').forEach(b => b.classList.toggle('active', b.dataset.device === device));
  document.getElementById('vp-browser').classList.toggle('mobile', device === 'mobile');
}

function _vpSetScheme(scheme) {
  _vp.scheme = scheme;
  document.querySelectorAll('#vp-scheme button').forEach(b => b.classList.toggle('active', b.dataset.scheme === scheme));
}

async function _vpClose() {
  if (_vp && JSON.stringify(_vp.drafts) !== _vp.saved) {
    if (!await confirm('Discard changes?', 'Your edits to the visitor pages are not saved.')) return;
  }
  document.getElementById('vp-backdrop').style.display = 'none';
}

function _initMaintModal() {
  const backdrop = document.getElementById('vp-backdrop');
  if (!backdrop) return;

  backdrop.addEventListener('mousedown', e => { if (e.target === backdrop) _vpClose(); });
  backdrop.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); _vpClose(); } });
  document.getElementById('vp-close').onclick = _vpClose;
  document.getElementById('vp-cancel').onclick = _vpClose;

  document.querySelector('.vp-tabs').addEventListener('click', e => {
    const b = e.target.closest('[data-page]');
    if (b) _vpShowPage(b.dataset.page);
  });

  // Text fields
  document.querySelectorAll('#vp-form [data-key]').forEach(el => {
    if (el.classList.contains('segmented')) return;
    el.addEventListener('input', () => {
      if (el.dataset.key === 'color') {
        _vpSetColor(el.value.trim());
        return;
      }
      _vp.drafts[_vp.page][el.dataset.key] = el.value;
      _vpChanged();
    });
  });

  // Colour
  document.getElementById('vp-swatches').addEventListener('click', e => {
    const b = e.target.closest('[data-color]');
    if (b) _vpSetColor(b.dataset.color);
  });
  document.getElementById('vp-color-picker').addEventListener('input', e => _vpSetColor(e.target.value));

  // Segmented controls
  document.querySelectorAll('.vp-seg').forEach(seg => seg.addEventListener('click', e => {
    const b = e.target.closest('button[data-value]');
    if (!b) return;
    _vpSetSeg(seg, b.dataset.value);
    _vp.drafts[_vp.page][seg.dataset.key] = b.dataset.value;
    _vpChanged();
  }));

  // Logo
  const fileInput = document.getElementById('vp-logo-file');
  document.getElementById('vp-logo-upload').onclick = () => fileInput.click();
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    fileInput.value = '';
    if (!file) return;
    if (file.size > 512 * 1024) { toast('Logo must be under 512 KB', 'error'); return; }
    const reader = new FileReader();
    reader.onload = () => {
      _vp.drafts[_vp.page].logo_data = reader.result;
      _vpRenderLogo();
      _vpChanged();
    };
    reader.readAsDataURL(file);
  });
  document.getElementById('vp-logo-remove').onclick = () => {
    _vp.drafts[_vp.page].logo_data = null;
    _vpRenderLogo();
    _vpChanged();
  };

  // Apply branding & style to every page
  document.getElementById('vp-apply-all').onclick = () => {
    const src = _vp.drafts[_vp.page];
    VP_PAGES.forEach(t => VP_SHARED_KEYS.forEach(k => { _vp.drafts[t][k] = src[k]; }));
    _vpUpdateDirty();
    toast('Branding and style copied to all four pages');
  };

  // Custom HTML
  document.getElementById('vp-custom-on').addEventListener('change', e => {
    const d = _vp.drafts[_vp.page];
    if (!e.target.checked) d.custom_html = '';
    else d.custom_html = document.getElementById('vp-f-custom').value;
    _vpSyncCustom();
    _vpChanged();
  });
  document.getElementById('vp-f-custom').addEventListener('input', e => {
    _vp.drafts[_vp.page].custom_html = e.target.value;
    _vpChanged();
  });

  // Preview controls
  document.getElementById('vp-device').addEventListener('click', e => {
    const b = e.target.closest('[data-device]');
    if (b) _vpSetDevice(b.dataset.device);
  });
  document.getElementById('vp-scheme').addEventListener('click', e => {
    const b = e.target.closest('[data-scheme]');
    if (!b) return;
    _vpSetScheme(b.dataset.scheme);
    _vpPreview(true);
  });

  document.getElementById('vp-save').onclick = saveMaintenancePages;
}

function _updateMaintBadges() {
  const isMaint = !!app.maintenance_mode;
  const isUpdate = !!app.update_mode;
  const set = (id, text, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.className = `maint-row-badge ${cls}`;
  };
  set('maint-downtime-badge', isMaint ? 'On' : 'Off', isMaint ? 'maint-badge--red' : 'maint-badge--off');
  set('maint-update-badge', isUpdate ? 'On' : 'Off', isUpdate ? 'maint-badge--orange' : 'maint-badge--off');
  set('maint-restart-badge', 'Auto', 'maint-badge--blue');
  set('maint-starting-badge', 'Auto', 'maint-badge--blue');

  const tabState = { downtime: isMaint ? 'On' : '', update: isUpdate ? 'On' : '' };
  document.querySelectorAll('.vp-tab-state[data-state]').forEach(el => {
    el.textContent = tabState[el.dataset.state];
    el.classList.toggle('on', !!tabState[el.dataset.state]);
  });
}

async function saveMaintenancePages() {
  const btn = document.getElementById('vp-save');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const payload = Object.fromEntries(VP_PAGES.map(t => [`${t}_page`, _vpPayloadPage(_vp.drafts[t])]));
  try {
    const res = await api.saveMaintenancePages(APP_ID, payload);
    app = await api.getApp(APP_ID);
    _vp.saved = JSON.stringify(_vp.drafts);
    _updateMaintBadges();
    _vpUpdateDirty();
    if (res.ok) toast('Visitor pages saved');
    else toast(`Saved, but nginx wasn't updated: ${res.message || 'unknown error'}`, 'warn');
  } catch (e) {
    toast(e.message, 'error');
    btn.disabled = false;
  } finally {
    btn.textContent = 'Save pages';
  }
}

function addDomainRow(container, value = '') {
  const row = document.createElement('div');
  row.className = 'env-row';
  row.innerHTML = `
    <input class="input" placeholder="sub.example.com" value="${escAttr(value)}" data-domain-val style="flex:1" />
    <button type="button" class="btn-remove" title="Remove">${icon.trash}</button>`;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
}

function addEnvRow(container, key = '', value = '') {
  const row = document.createElement('div');
  row.className = 'env-row';
  // If key exists but value is empty, this is a write-only existing var — show placeholder hint
  const valuePlaceholder = key && value === '' ? '(unchanged — type to update)' : 'value';
  row.innerHTML = `
    <input class="input input-mono" placeholder="KEY" data-env-key />
    <input class="input input-mono" placeholder="${valuePlaceholder}" data-env-val />
    <button type="button" class="btn-remove" title="Remove">${icon.trash}</button>`;
  // Set via .value so &, quotes and newlines (e.g. from an imported .env) survive
  row.querySelector('[data-env-key]').value = key;
  row.querySelector('[data-env-val]').value = value;
  row.querySelector('.btn-remove').addEventListener('click', () => row.remove());
  container.appendChild(row);
  return row;
}

function escAttr(s) {
  return (s || '').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

/* ─── INSTANCES TAB ─────────────────────────────────────────────────────── */
let _instancesRefreshTimer = null;

const _sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function _waitForInstanceSync(predicate, timeoutMs = 15000, intervalMs = 700) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const instances = await api.listInstances(APP_ID);
      if (predicate(instances || [])) return true;
    } catch {
      // Keep polling; transient API hiccups should not fail sync wait immediately.
    }
    await _sleep(intervalMs);
  }
  return false;
}

async function initInstances() {
  const wrap = document.getElementById('instances-table-wrap');
  if (!wrap) return;
  const pendingRemovals = new Set();
  let pendingCreate = false;

  async function renderInstances() {
    let instances = [], instStats = {};
    try {
      [instances, instStats] = await Promise.all([
        api.listInstances(APP_ID),
        api.getInstanceStats(APP_ID).catch(() => ({})),
      ]);
    } catch (e) {
      wrap.innerHTML = `<div style="color:var(--red);padding:12px;font-size:13px">Failed to load instances: ${e.message}</div>`;
      return;
    }

    if (!instances.length) {
      wrap.innerHTML = '<div style="color:var(--text-muted);padding:12px;font-size:13px">No instances found.</div>';
      return;
    }

    const cards = instances.map((inst, idx) => {
      const removePending = pendingRemovals.has(inst.id);
      const isRunning  = inst.status === 'running';
      const isStarting = inst.status === 'starting';
      const isError    = inst.status === 'error';

      const statusColor = isRunning ? 'var(--green)' : isError ? 'var(--red)' : isStarting ? 'var(--yellow)' : 'var(--text-muted)';
      const statusBg    = isRunning ? 'var(--green-bg)' : isError ? 'var(--red-bg)' : isStarting ? 'var(--yellow-bg)' : 'var(--bg-muted)';
      const statusDot   = isRunning ? 'var(--green)' : isError ? 'var(--red)' : isStarting ? 'var(--yellow)' : 'var(--text-muted)';

      const nodeName = inst.node_name || 'Primary Node';

      // Uptime — only meaningful while running. Prefer the container's real
      // start time from Docker stats; fall back to the row's updated_at.
      // DB timestamps are UTC without Z suffix; append Z so browser parses as UTC.
      let uptimeStr = '—';
      const statUptime = instStats[inst.id]?.uptime_seconds;
      const uptimeSrc = isRunning ? (inst.updated_at || inst.created_at) : null;
      if (isRunning && statUptime > 0) {
        uptimeStr = fmtUptime(statUptime);
      } else if (uptimeSrc) {
        const ts = uptimeSrc.endsWith('Z') || uptimeSrc.includes('+') ? uptimeSrc : uptimeSrc + 'Z';
        const diffMs = Date.now() - new Date(ts).getTime();
        if (diffMs > 0) uptimeStr = fmtUptime(Math.floor(diffMs / 1000));
      }

      // Connection info: local / tunnel with port / no tunnel
      let connHtml;
      if (inst.node_is_local) {
        connHtml = `<span style="font-size:12px;color:var(--text-muted)">local</span>`;
      } else if (inst.tunnel_connected && inst.tunnel_port) {
        connHtml = `<span style="font-size:12px;color:var(--green);display:flex;align-items:center;gap:4px">
          <svg width="7" height="7" viewBox="0 0 7 7"><circle cx="3.5" cy="3.5" r="3.5" fill="currentColor"/></svg>
          tunnel :${inst.tunnel_port}
        </span>`;
      } else if (inst.tunnel_connected) {
        connHtml = `<span style="font-size:12px;color:var(--green)">tunnel</span>`;
      } else {
        connHtml = `<span style="font-size:12px;color:var(--red)">no tunnel</span>`;
      }

      // Startup progress steps
      let startupHtml = '';
      if (isStarting) {
        const sub = inst.substatus;
        // Steps in the order they always appear — downloading/building may be skipped (cached image)
        const steps = [
          { key: 'downloading',        label: 'Downloading source' },
          { key: 'building_image',     label: 'Building image' },
          { key: 'creating_container', label: 'Creating container' },
          { key: 'waiting',            label: 'Starting app' },
        ];
        // Determine which step is active and which are done
        const foundIdx = sub ? steps.findIndex(s => s.key === sub) : -1;
        const activeIdx = foundIdx >= 0 ? foundIdx : 0;
        startupHtml = `
          <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border-muted)">
            <div style="font-size:9px;text-transform:uppercase;letter-spacing:.04em;color:var(--text-muted);margin-bottom:6px">Starting up</div>
            <div style="display:flex;flex-direction:column;gap:5px">
              ${steps.map((step, i) => {
                const isDone   = i < activeIdx;
                const isActive = i === activeIdx;
                return `<div style="display:flex;align-items:center;gap:7px">
                  ${isActive
                    ? `<svg style="animation:spin 0.8s linear infinite;flex-shrink:0;color:var(--yellow)" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>`
                    : isDone
                      ? `<svg style="flex-shrink:0;color:var(--green)" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`
                      : `<div style="width:9px;height:9px;border-radius:50%;border:1.5px solid var(--border);flex-shrink:0"></div>`
                  }
                  <span style="font-size:11px;color:${isActive ? 'var(--text-primary)' : isDone ? 'var(--green)' : 'var(--text-muted)'};${isActive ? 'font-weight:500' : ''}">${step.label}</span>
                </div>`;
              }).join('')}
            </div>
          </div>`;
      }

      // Live metrics
      const snap = instStats[String(inst.id)];
      let usageText = '—';
      if (snap && isRunning) {
        const parts = [];
        if (snap.cpu_percent != null) parts.push(`${snap.cpu_percent.toFixed(1)}%`);
        if (snap.memory_mb != null) {
          const mem = Math.round(snap.memory_mb);
          parts.push(mem >= 1024 ? `${(mem / 1024).toFixed(1)} GB` : `${mem} MB`);
        }
        usageText = parts.join(' · ') || '—';
      } else if (isRunning) {
        usageText = 'collecting…';
      }

      const cpuLimit = inst.docker_cpu_limit != null ? `${inst.docker_cpu_limit} CPU` : null;
      const memLimit = inst.docker_memory_limit_mb != null ? `${inst.docker_memory_limit_mb}MB` : null;
      const limitsText = [cpuLimit, memLimit].filter(Boolean).join(' · ');
      const containerShort = inst.container_id ? inst.container_id.slice(0, 12) : null;

      return `
      <div class="inst-row"${isStarting ? ' data-starting="1"' : ''}>
        <div class="inst-main">
          <span class="inst-id">#${inst.id}</span>
          <span class="inst-status" style="color:${statusColor}"><span class="dot" style="background:${statusDot}"></span>${inst.status}</span>
          <span class="inst-node">${escHtml(nodeName)}</span>
          <span class="inst-port">:${inst.external_port || '—'}</span>
          <span class="inst-conn">${connHtml}</span>
          <span class="inst-usage">${usageText}</span>
          <span class="inst-uptime">${uptimeStr}</span>
          <span class="inst-actions">
            <button class="btn btn-sm inst-restart-btn" data-perm="apps.restart" data-id="${inst.id}" ${removePending ? 'disabled' : ''}>Restart</button>
            <button class="btn btn-sm btn-danger inst-remove-btn" data-perm="apps.scale" data-id="${inst.id}" ${removePending ? 'disabled' : ''}>${removePending ? `${spinner} Removing…` : 'Remove'}</button>
          </span>
        </div>
        ${limitsText || containerShort ? `<div class="inst-meta">${limitsText ? `<span>${limitsText}</span>` : ''}${containerShort ? `<span class="mono" title="${escHtml(inst.container_id || '')}">${containerShort}</span>` : ''}</div>` : ''}
        ${startupHtml}
        ${inst.last_error ? `<div class="inst-error">${escHtml(inst.last_error)}</div>` : ''}
      </div>`;
    }).join('');

    wrap.innerHTML = `
      <div class="inst-list">
        <div class="inst-main inst-head" aria-hidden="true">
          <span>Instance</span><span>Status</span><span>Node</span><span>Port</span><span>Connection</span><span>CPU · Memory</span><span>Uptime</span><span></span>
        </div>
        ${cards}
      </div>`;
    if (window._applyPermVisibility) window._applyPermVisibility(wrap);
    _rescheduleInstancesTimer();

    wrap.querySelectorAll('.inst-restart-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const instanceId = parseInt(btn.dataset.id, 10);
        const ok = await confirm('Restart instance?', 'The instance container will be stopped and restarted.');
        if (!ok) return;
        btn.disabled = true;
        btn.innerHTML = `${spinner} Restarting…`;
        try {
          await api.restartInstance(APP_ID, instanceId);
          toast('Instance restarting…');
          await renderInstances();
        } catch (e) {
          toast(e.message, 'error');
          btn.disabled = false;
          btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg> Restart`;
        }
      });
    });

    wrap.querySelectorAll('.inst-remove-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const instanceId = parseInt(btn.dataset.id, 10);
        if (pendingRemovals.has(instanceId)) return;
        const ok = await confirm('Remove instance?', 'The instance container will be stopped and removed.');
        if (!ok) return;
        pendingRemovals.add(instanceId);
        clearInterval(_instancesRefreshTimer);
        await renderInstances();
        try {
          await api.deleteInstance(APP_ID, instanceId);
          await _waitForInstanceSync(instances => !instances.some(i => i.id === instanceId));
          pendingRemovals.delete(instanceId);
          app = await api.getApp(APP_ID);
          updateHeaderStatus();
          renderHeader();
          await renderInstances();
          toast('Instance removed');
        } catch (e) {
          pendingRemovals.delete(instanceId);
          await renderInstances();
          toast(e.message, 'error');
        }
      });
    });

  }

  // After each render, check instance states and reschedule the timer accordingly.
  // renderInstances already fetches instances internally, so we peek at the DOM
  // to detect starting states without an extra API call.
  function _rescheduleInstancesTimer() {
    clearInterval(_instancesRefreshTimer);
    const hasStarting = wrap.querySelector('[data-starting="1"]') !== null;
    _instancesRefreshTimer = setInterval(renderInstances, hasStarting ? 1500 : 5000);
  }

  await renderInstances();

  const refreshBtn = document.getElementById('btn-instances-refresh');
  if (refreshBtn) refreshBtn.onclick = renderInstances;

  const addBtn = document.getElementById('btn-add-instance');
  if (addBtn) {
    addBtn.onclick = async () => {
      let nodes = [];
      try {
        nodes = await api.listNodes();
      } catch {
        toast('Failed to load nodes', 'error');
        return;
      }
      const available = nodes.filter(n => n.enabled && n.status === 'online');

      const isStaticApp = app.app_type === 'static';
      const nodeOptions = [
        { value: '', name: 'primary', meta: 'This server' },
        ...available.filter(n => !n.is_local).map(n => ({
          value: String(n.id), name: n.name, meta: [n.metadata?.arch, n.public_host].filter(Boolean).join(' · ') || 'online',
        })),
      ];

      const result = await new Promise(resolve => {
        const backdrop = document.createElement('div');
        backdrop.className = 'dialog-backdrop';
        backdrop.innerHTML = `
          <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="inst-dlg-title">
            <div class="dialog-title" id="inst-dlg-title">Add instance</div>
            <div class="dialog-body">
              <p class="form-dialog-intro">Start one more copy of <strong>${escHtml(app.name)}</strong>. Traffic is spread over all running instances.</p>

              <fieldset class="form-block">
                <legend>Node</legend>
                <div class="choice-list" role="radiogroup">
                  ${nodeOptions.map((n, i) => `
                    <label class="choice">
                      <input type="radio" name="inst-node" value="${n.value}" ${i === 0 ? 'checked' : ''} />
                      <span class="choice-dot" aria-hidden="true"></span>
                      <span class="choice-text"><span>${escHtml(n.name)}</span><small>${escHtml(n.meta)}</small></span>
                    </label>`).join('')}
                </div>
              </fieldset>

              <fieldset class="form-block">
                <legend>Resources <small>leave empty for the app defaults</small></legend>
                <div class="form-pair">
                  <label class="unit-input"><span class="sr-only">CPU limit</span>
                    <input id="inst-cpu" class="input" type="number" min="0.1" step="0.1" placeholder="${app.docker_cpu_limit || '1.0'}" />
                    <span class="unit">CPU</span>
                  </label>
                  <label class="unit-input"><span class="sr-only">Memory limit</span>
                    <input id="inst-mem" class="input" type="number" min="16" step="16" placeholder="${app.docker_memory_limit_mb || '512'}" />
                    <span class="unit">MB</span>
                  </label>
                </div>
              </fieldset>

              <details class="form-advanced">
                <summary>Advanced</summary>
                ${isStaticApp ? '' : `
                <label class="toggle-row form-toggle">
                  <span><span class="field-label">Read-only root filesystem</span><span class="field-hint">Only mounted paths are writable</span></span>
                  <span class="toggle"><input type="checkbox" id="inst-readonly" ${app.docker_read_only_root ? 'checked' : ''} /><span class="toggle-slider"></span></span>
                </label>`}
                <label class="toggle-row form-toggle">
                  <span><span class="field-label">Tmpfs at /tmp</span><span class="field-hint">In-memory scratch space</span></span>
                  <span class="toggle"><input type="checkbox" id="inst-tmpfs" ${app.docker_tmpfs_enabled ? 'checked' : ''} /><span class="toggle-slider"></span></span>
                </label>
                <label class="unit-input form-tmpfs-size"><span class="sr-only">Tmpfs size</span>
                  <input id="inst-tmpfs-size" class="input" type="number" min="1" placeholder="${app.docker_tmpfs_size_mb || 64}" />
                  <span class="unit">MB</span>
                </label>
              </details>
            </div>
            <div class="dialog-actions">
              <button class="btn" id="inst-dlg-cancel">Cancel</button>
              <button class="btn btn-primary" id="inst-dlg-ok">Start instance</button>
            </div>
          </div>`;
        document.body.appendChild(backdrop);
        const ok = () => {
          const nodeVal = backdrop.querySelector('input[name="inst-node"]:checked')?.value || '';
          const cpu = parseFloat(backdrop.querySelector('#inst-cpu').value);
          const mem = parseInt(backdrop.querySelector('#inst-mem').value, 10);
          const readonly = !!backdrop.querySelector('#inst-readonly')?.checked;
          const tmpfs = backdrop.querySelector('#inst-tmpfs').checked;
          const tmpfsSz = parseInt(backdrop.querySelector('#inst-tmpfs-size').value, 10);
          backdrop.remove();
          resolve({
            nodeId: nodeVal ? parseInt(nodeVal, 10) : null,
            cpu: Number.isFinite(cpu) ? cpu : null,
            mem: Number.isInteger(mem) ? mem : null,
            readonly,
            tmpfs,
            tmpfsSz: Number.isInteger(tmpfsSz) ? tmpfsSz : null,
          });
        };
        backdrop.querySelector('#inst-dlg-ok').onclick = ok;
        backdrop.querySelector('#inst-dlg-cancel').onclick = () => {
          backdrop.remove();
          resolve(undefined);
        };
        backdrop.addEventListener('click', e => {
          if (e.target === backdrop) {
            backdrop.remove();
            resolve(undefined);
          }
        });
      });

      if (result === undefined) return;

      if (pendingCreate) return;
      pendingCreate = true;
      addBtn.disabled = true;
      addBtn.textContent = 'Starting...';
      try {
        const beforeInstances = await api.listInstances(APP_ID).catch(() => []);
        const beforeIds = new Set((beforeInstances || []).map(i => i.id));
        await api.scaleApp(APP_ID, {
          node_id: result.nodeId,
          docker_cpu_limit: result.cpu,
          docker_memory_limit_mb: result.mem,
          docker_read_only_root: result.readonly,
          docker_tmpfs_enabled: result.tmpfs,
          docker_tmpfs_size_mb: result.tmpfsSz,
        });
        const started = await _waitForInstanceSync(instances => instances.some(i => !beforeIds.has(i.id)));
        app = await api.getApp(APP_ID);
        updateHeaderStatus();
        renderHeader();
        await renderInstances();
        if (started) {
          toast('Instance started');
        } else {
          toast('Instance is still provisioning');
        }
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        pendingCreate = false;
        addBtn.disabled = false;
        addBtn.textContent = 'Add Instance';
      }
    };
  }
}



async function saveSettings() {
  const btn = document.getElementById('btn-save');
  btn.disabled = true;
  btn.innerHTML = `${spinner} Saving…`;

  // Build env var update: send new/changed values + the full set of remaining keys
  // so the backend knows which keys to remove (those not in env_var_keys).
  const env_vars = {};
  const env_var_keys = [];
  document.querySelectorAll('#cfg-env-rows .env-row').forEach(row => {
    const k = row.querySelector('[data-env-key]').value.trim();
    const v = row.querySelector('[data-env-val]').value;
    if (!k) return;
    env_var_keys.push(k);
    if (v !== '') env_vars[k] = v;  // only send value if user typed something
  });

  const tokenId = document.getElementById('cfg-token-id')?.value?.trim();
  const token   = document.getElementById('cfg-token')?.value?.trim();
  const dockerCpu = parseFloat(document.getElementById('cfg-docker-cpu').value);
  const dockerMemory = parseInt(document.getElementById('cfg-docker-memory').value, 10);
  const dockerTmpfsSize = parseInt(document.getElementById('cfg-docker-tmpfs-size').value, 10);

  const allDomainInputs = [...document.querySelectorAll('#cfg-domains-rows [data-domain-val]')]
                            .map(i => i.value.trim()).filter(Boolean);
  const payload = {
    start_command:  document.getElementById('cfg-cmd').value.trim()    || null,
    build_command:  document.getElementById('cfg-build').value.trim(),  // "" clears it
    port:           parseInt(document.getElementById('cfg-port').value) || null,
    domain:         allDomainInputs[0] || null,
    extra_domains:  allDomainInputs.slice(1),
    redirect_domains: [...document.querySelectorAll('#cfg-redirect-domains-rows [data-domain-val]')]
                        .map(i => i.value.trim()).filter(Boolean),
    ssl_cert_path:  document.getElementById('cfg-cert').value.trim()   || null,
    ssl_key_path:   document.getElementById('cfg-key').value.trim()    || null,
    no_web:         !!app?.no_web,
    auto_start:     document.getElementById('cfg-autostart').checked,
    restart_policy: document.getElementById('cfg-restart-policy').value,
    docker_cpu_limit: Number.isFinite(dockerCpu) ? dockerCpu : null,
    docker_memory_limit_mb: Number.isInteger(dockerMemory) ? dockerMemory : null,
    docker_read_only_root: document.getElementById('cfg-docker-readonly').checked,
    docker_tmpfs_enabled: document.getElementById('cfg-docker-tmpfs-enabled').checked,
    docker_tmpfs_size_mb: Number.isInteger(dockerTmpfsSize) ? dockerTmpfsSize : null,
    autoscale_enabled:      document.getElementById('cfg-autoscale-enabled').checked,
    autoscale_min_replicas: parseInt(document.getElementById('cfg-autoscale-min').value) || 1,
    autoscale_max_replicas: parseInt(document.getElementById('cfg-autoscale-max').value) || 4,
    autoscale_cpu_target:   parseFloat(document.getElementById('cfg-autoscale-cpu').value) || 70,
    env_vars,
    env_var_keys,
    ...(tokenId ? { github_token_id: tokenId } : token ? { github_token: token } : {}),
  };

  try {
    app = await api.updateApp(APP_ID, payload);
    if (app?.pending_sync) {
      toast(app.message || 'Settings saved and queued for node sync');
    } else {
      toast('Settings saved');
    }
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `${icon.save} Save Settings`;
  }
}

async function openNginxModal() {
  const modal    = document.getElementById('nginx-modal');
  const textarea = document.getElementById('nginx-config-textarea');
  const pathEl   = document.getElementById('nginx-config-path');
  const badge    = document.getElementById('nginx-status-badge');
  const msgEl    = document.getElementById('nginx-save-msg');
  msgEl.style.display = 'none';
  textarea.value = 'Loading…';
  modal.style.display = 'flex';

  try {
    const data = await api.getNginxConfig(APP_ID);
    pathEl.textContent = data.path;
    badge.textContent  = data.active ? '● Active' : data.exists ? '○ Inactive' : '○ Not created';
    badge.style.color  = data.active ? 'var(--green)' : 'var(--text-muted)';
    textarea.value = data.content || '# No config yet — fill in domain/port in Settings and save to generate one';
  } catch (e) {
    textarea.value = `Error: ${e.message}`;
  }

  const saveBtn = document.getElementById('nginx-save');
  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    msgEl.style.display = 'none';
    try {
      const res = await api.saveNginxConfig(APP_ID, textarea.value);
      msgEl.textContent = res.ok ? 'Saved & nginx reloaded successfully.' : `Error: ${res.message}`;
      msgEl.style.display = 'block';
      msgEl.style.color = res.ok ? 'var(--green)' : 'var(--red)';
      if (res.ok) { badge.textContent = '● Active'; badge.style.color = 'var(--green)'; }
    } catch (e) {
      msgEl.textContent = e.message; msgEl.style.display = 'block'; msgEl.style.color = 'var(--red)';
    } finally {
      saveBtn.disabled = false;
    }
  };

  document.getElementById('nginx-close').onclick = () => { modal.style.display = 'none'; };
  modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
}

async function tileAction(endpoint, label) {
  const tileId = endpoint === 'pull' ? 'tile-pull' : 'tile-rebuild';
  const tile = document.getElementById(tileId);           // optional: Settings → Actions tile
  const tileIcon = tile?.querySelector('.action-tile-icon');
  const origIcon = tileIcon?.innerHTML;
  if (tile) tile.disabled = true;
  if (tileIcon) tileIcon.innerHTML = spinner;
  const logsTitle = endpoint === 'pull' ? 'Pull + Rebuild Logs' : 'Rebuild Logs';
  let logDialog;

  try {
    let streamPath, body = null;
    if (endpoint === 'pull') {
      const selectedCommit = await openCommitPicker();
      if (selectedCommit === null) {
        return;
      }
      streamPath = `/apps/${APP_ID}/pull/stream`;
      body = selectedCommit ? { commit: selectedCommit } : null;
    } else {
      streamPath = `/apps/${APP_ID}/rebuild/stream`;
    }

    logDialog = openActionLogsDialog(logsTitle);
    await api.streamAction(streamPath, body, line => logDialog.append(line));
    logDialog.setStatus('Done');
  } catch (e) {
    if (logDialog) {
      logDialog.append(`[Error] ${e.message}`);
      logDialog.setStatus('Failed');
    }
    toast(e.message, 'error');
  } finally {
    if (tile) tile.disabled = false;
    if (tileIcon) tileIcon.innerHTML = origIcon;
  }
}

/** Pull a commit and deploy it in one go: strategy = rebuild | rolling | blue_green. */
async function pullAndDeploy(strategy) {
  const commit = await openCommitPicker();
  if (commit === null) return;
  const titles = { rebuild: 'Pull & build', rolling: 'Pull & rolling deploy', blue_green: 'Pull & blue/green deploy' };
  const dlg = openActionLogsDialog(titles[strategy] || 'Deploy');
  try {
    const res = await api.streamAction(`/apps/${APP_ID}/deploy/stream`, { commit: commit || null, strategy }, line => dlg.append(line));
    dlg.setStatus('Done');
    toast(res?.message || 'Deploy finished');
  } catch (e) {
    dlg.append(`[Error] ${e.message}`);
    dlg.setStatus('Failed');
    toast(e.message, 'error');
  } finally {
    try {
      app = await api.getApp(APP_ID);
      updateHeaderStatus();
    } catch { /* keep the old header */ }
  }
}

function openActionLogsDialog(title) {
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern action-log-dialog" style="max-width:760px;width:min(760px,92vw)">
      <div class="dialog-title">${escHtml(title)}</div>
      <div class="dialog-body action-log-body">
        <pre class="action-log-pre" id="action-log-pre"></pre>
      </div>
      <div class="dialog-actions action-log-actions">
        <div class="action-log-status" id="action-log-status">Running…</div>
        <button class="btn btn-primary" id="action-log-close">Close</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const pre = backdrop.querySelector('#action-log-pre');
  const status = backdrop.querySelector('#action-log-status');
  const close = () => backdrop.remove();
  backdrop.querySelector('#action-log-close').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });

  return {
    append(line) {
      pre.textContent += `${line}\n`;
      pre.scrollTop = pre.scrollHeight;
    },
    setStatus(text) {
      status.textContent = text;
    },
  };
}

async function openCommitPicker() {
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern commit-picker-dialog" style="max-width:760px;width:min(760px,92vw)">
      <div class="dialog-title">Select Commit</div>
      <div class="dialog-body commit-picker-body">
        <div class="commit-picker-intro">
          Choose the latest commit or pin this app to a specific recent commit before rebuilding.
        </div>
        <div class="commit-picker-toolbar">
          <button class="btn btn-secondary btn-sm" id="commit-latest">Latest on current branch</button>
          <span id="commit-picker-sync" class="commit-picker-sync">Loading recent commits...</span>
        </div>
        <div class="commit-picker-list" id="commit-picker-list">
          <div class="commit-picker-loading">Loading commits…</div>
        </div>
      </div>
      <div class="dialog-actions">
        <button class="btn btn-secondary" id="commit-cancel">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  let resolvePicker = null;
  const renderCommitRows = (listEl, commits) => {
    if (!commits.length) {
      listEl.innerHTML = '<div class="commit-picker-empty">No recent commits found.</div>';
      return;
    }

    listEl.innerHTML = commits.map(commit => `
      <button class="commit-row" data-commit="${commit.hash}">
        <div class="commit-row-top">
          <span class="commit-hash">${commit.short_hash}</span>
          <span class="commit-time">${commit.relative_time}</span>
        </div>
        <div class="commit-subject">${escHtml(commit.subject)}</div>
        <div class="commit-author">${escHtml(commit.author)}</div>
      </button>`).join('');

    listEl.scrollTop = 0;
    listEl.querySelectorAll('.commit-row').forEach(row => {
      row.onclick = () => {
        if (resolvePicker) resolvePicker(close(row.dataset.commit));
      };
    });
  };

  let closed = false;
  const close = (value) => {
    closed = true;
    backdrop.remove();
    return value;
  };

  return await new Promise(async resolve => {
    resolvePicker = resolve;
    backdrop.addEventListener('click', e => { if (e.target === backdrop) resolve(close(null)); });
    backdrop.querySelector('#commit-cancel').onclick = () => resolve(close(null));
    backdrop.querySelector('#commit-latest').onclick = () => resolve(close(''));

    try {
      const list = backdrop.querySelector('#commit-picker-list');
      const sync = backdrop.querySelector('#commit-picker-sync');

      const data = await api.listCommits(APP_ID, 40, true);
      if (!closed) {
        renderCommitRows(list, data.commits || []);
        sync.textContent = `Showing ${data.ref || 'latest'} commits`;
      }
    } catch (e) {
      if (!closed) {
        backdrop.querySelector('#commit-picker-list').innerHTML = `<div class="commit-picker-empty" style="color:var(--red)">${escHtml(e.message)}</div>`;
        const sync = backdrop.querySelector('#commit-picker-sync');
        if (sync) sync.textContent = 'Failed to load commits';
      }
    }
  });
}
