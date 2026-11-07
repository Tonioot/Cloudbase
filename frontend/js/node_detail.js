import { api, wsNodeEvents, wsSystemStats } from './api.js';
import { badge, toast, confirm, prompt, spinner, fmtDate, timeAgo, fmtUptime, cssVar, sparkPoints } from './utils.js';
import { setCrumbs } from './shell.js';

const params  = new URLSearchParams(location.search);
const NODE_ID = parseInt(params.get('id'));

let node = null;
let workloads = { instances: [], databases: [] };
let lastPing = null;               // { ms } | { unreachable: true } | null
let hasLoadedLogs = false;
let lastLogsText = '';
let latestLogsRequest = 0;
let activeTab = 'log';

// ── Metrics ───────────────────────────────────────────────────────────────────
// One request gives every series (usage, network and disk rates, load) at the resolution the range needs.
let metricSamples = [];
let _nodeRangeHours = 1;
const METRICS_REFRESH_MS = 30000;      // the server samples every 30 seconds

const fmtPct = v => `${v.toFixed(v < 10 ? 1 : 0)}%`;
const fmtLoad = v => v.toFixed(v < 10 ? 2 : 1);
function fmtRate(v) {
    if (v < 1024) return `${Math.round(v)} B/s`;
    if (v < 1048576) return `${(v / 1024).toFixed(v < 10240 ? 1 : 0)} KB/s`;
    return `${(v / 1048576).toFixed(v < 10485760 ? 1 : 0)} MB/s`;
}
const CHARTS = [
    { id: 'cpu',  title: 'CPU',          unit: '%',    fmt: fmtPct,  series: [{ key: 'cpu',  color: '--accent' }], pct: true },
    { id: 'mem',  title: 'Memory',       unit: '%',    fmt: fmtPct,  series: [{ key: 'mem',  color: '--purple' }], pct: true },
    { id: 'disk', title: 'Disk usage',   unit: '%',    fmt: fmtPct,  series: [{ key: 'disk', color: '--yellow' }], pct: true },
    { id: 'net',  title: 'Network',      unit: 'B/s',  fmt: fmtRate, series: [{ key: 'net_in', color: '--accent', label: 'In' }, { key: 'net_out', color: '--purple', label: 'Out' }] },
    { id: 'io',   title: 'Disk reads and writes', unit: 'B/s', fmt: fmtRate, series: [{ key: 'disk_read', color: '--accent', label: 'Read' }, { key: 'disk_write', color: '--yellow', label: 'Write' }] },
    { id: 'load', title: 'Load average', unit: '1 min', fmt: fmtLoad, series: [{ key: 'load', color: '--green' }] },
];
const RANGE_LABELS = { 1: 'Last hour', 24: 'Last 24 hours', 168: 'Last 7 days' };

export async function initNodePage() {
    if (!NODE_ID || isNaN(NODE_ID)) {
        window.location.href = '/';
        return;
    }

    try {
        node = (await api.listNodes()).find(n => n.id === NODE_ID);
        if (!node) { window.location.href = '/'; return; }
    } catch (err) {
        toast(err.message, 'error');
        setTimeout(() => { window.location.href = '/'; }, 1500);
        return;
    }

    renderAll();
    buildCharts();
    loadMetrics();
    refreshWorkloads();
    loadLogs();
    setupWebSockets();

    document.getElementById('btn-rename').onclick = renameNode;
    document.getElementById('btn-toggle-enable').onclick = toggleEnable;
    document.getElementById('btn-delete').onclick = deleteNode;
    document.getElementById('btn-refresh-logs').onclick = () => (activeTab === 'log' ? loadLogs() : loadHistory());
    document.getElementById('btn-docker-cleanup').onclick = cleanupDocker;
    initTabs();

    setInterval(refreshNode, 8000);
    setInterval(loadMetrics, METRICS_REFRESH_MS);
    setInterval(refreshWorkloads, 6000);
    setInterval(() => { if (activeTab === 'log') loadLogs(); }, 12000);
    setInterval(() => { if (activeTab === 'commands') loadHistory(); }, 10000);

    if (!node.is_local) autoPing();
    else wsSystemStats(data => updateMetrics({ cpu_percent: data.cpu_percent, memory_percent: data.memory_percent, disk_percent: data.disk_percent }));
}

/** Everything that comes from the node itself, drawn again. */
function renderAll() {
    renderHeader();
    renderTiles();
    renderAbout();
}

window.addEventListener('resize', () => drawCharts());

async function refreshNode() {
    try {
        const fresh = (await api.listNodes()).find(n => n.id === NODE_ID);
        if (!fresh) return;
        const wasOnline = node.status === 'online';
        node = fresh;
        renderAll();
        if (wasOnline !== (node.status === 'online')) _setChartsOfflineState(node.status !== 'online');
    } catch { /* the next round tries again */ }
}

async function refreshWorkloads() {
    try {
        workloads = await api.getNodeWorkloads(NODE_ID);
        renderRunning();
    } catch { /* keep what is shown */ }
}

async function autoPing() {
    const run = async () => {
        try {
            const r = await api.pingNode(NODE_ID);
            lastPing = r.reachable ? { ms: r.latency_ms } : { unreachable: true };
        } catch {
            lastPing = null;
        }
        renderTiles();
    };
    await run();
    setInterval(run, 15000);
}

/* ─── Header ───────────────────────────────────────────────────────────── */
function renderHeader() {
    document.getElementById('node-name').textContent = node.name;
    setCrumbs([{ label: 'Nodes', href: '/#nodes-section' }, node.name]);
    document.title = `${node.name} — Node — Cloudbase`;

    const online = node.status === 'online';
    const badgeEl = document.getElementById('node-status-badge');
    if (!node.enabled && !node.is_local) badgeEl.innerHTML = '<span class="badge badge-stopped">Disabled</span>';
    else badgeEl.innerHTML = `<span class="badge ${online ? 'badge-running' : 'badge-error'}">${online ? 'Online' : 'Offline'}</span>`;

    const meta = node.metadata || {};
    const role = node.is_local ? 'Primary node — this server' : node.role === 'hybrid' ? 'Hybrid node' : 'Node';
    document.getElementById('node-meta').textContent = [
        role,
        node.agent_version ? `agent ${node.agent_version}` : null,
        meta.os_short || meta.os || null,
        meta.arch || null,
    ].filter(Boolean).join(' · ');

    const toggleBtn = document.getElementById('btn-toggle-enable');
    if (node.is_local) {
        document.getElementById('btn-rename').style.display = 'none';
        toggleBtn.style.display = 'none';
        document.getElementById('btn-delete').style.display = 'none';
    } else {
        toggleBtn.textContent = node.enabled ? 'Disable' : 'Enable';
    }
}

/* ─── At a glance ──────────────────────────────────────────────────────── */
function setTile(id, { value, sub, spark }) {
    const tile = document.getElementById(id);
    if (!tile) return;
    const line = tile.querySelector('.kpi-spark polyline');
    if (line) line.setAttribute('points', spark ? sparkPoints(spark) : '');
    tile.querySelector('.kpi-value strong').textContent = value;
    tile.querySelector('.kpi-sub').textContent = sub || '\u00a0';
}

const pctText = p => (p == null ? '—' : p.toFixed(p < 10 ? 1 : 0));
const gb = mb => (mb / 1024).toFixed(mb >= 10240 ? 0 : 1);

function renderTiles() {
    const m = node.node_metrics || {};
    const meta = node.metadata || {};
    const online = node.status === 'online' || node.is_local;

    const last = key => metricSamples.slice(-40).map(x => x[key]).filter(v => v != null);   // the last 20 minutes, small
    setTile('tile-cpu', {
        spark: last('cpu'),
        value: online ? pctText(m.cpu_percent) : '—',
        sub: meta.cpu_count ? `${meta.cpu_count_logical || meta.cpu_count} cores${meta.cpu_model ? ' · ' + meta.cpu_model : ''}` : '',
    });
    setTile('tile-mem', {
        spark: last('mem'),
        value: online ? pctText(m.memory_percent) : '—',
        sub: meta.ram_total_mb && m.memory_percent != null ? `${gb(meta.ram_total_mb * m.memory_percent / 100)} of ${gb(meta.ram_total_mb)} GB` : '',
    });
    setTile('tile-disk', {
        spark: last('disk'),
        value: online ? pctText(m.disk_percent) : '—',
        sub: meta.disk_total_gb && m.disk_percent != null ? `${(meta.disk_total_gb * m.disk_percent / 100).toFixed(0)} of ${meta.disk_total_gb} GB` : '',
    });
    renderConnection();
}

/** The connection tile: how the primary reaches this node, in words. */
function renderConnection() {
    const tile = document.getElementById('tile-net');
    const value = tile.querySelector('.kpi-value strong');
    const [sub1, sub2] = tile.querySelectorAll('.kpi-sub');
    const dot = state => `<span class="nd-link-dot ${state}"></span>`;
    const ping = lastPing?.ms != null ? `${lastPing.ms} ms` : lastPing?.unreachable ? 'not answering' : '';
    let html, line1 = '', line2 = '';

    if (node.is_local) {
        html = `${dot('up')}This server`;
        line1 = 'The primary itself';
    } else if (node.status !== 'online') {
        html = `${dot('down')}Offline`;
        line1 = `Last seen ${timeAgo(node.last_seen)}`;
        line2 = 'Its instances come back by themselves';
    } else if (node.wg_state === 'up') {
        html = `${dot('up')}Connected`;
        line1 = `Node network ${node.wg_ip || ''}`.trim();
        line2 = [node.wg_handshake_at ? `handshake ${timeAgo(node.wg_handshake_at)}` : null, ping || null].filter(Boolean).join(' \u00b7 ');
    } else if (node.wg_state === 'down') {
        html = `${dot('down')}Network down`;
        line1 = 'Its instances are out of rotation';
        line2 = 'The agent is still connected';
    } else {
        html = `${dot('busy')}Not on the node network`;
        line1 = 'Run on the node: cloudbase wireguard';
        line2 = 'It can\u2019t run instances until then';
    }
    value.innerHTML = html;
    sub1.textContent = line1 || '\u00a0';
    sub2.textContent = line2 || '\u00a0';
}

/* ─── What runs here ───────────────────────────────────────────────────── */
function renderRunning() {
    const list = document.getElementById('run-list');
    const { instances, databases } = workloads;
    document.getElementById('run-count').textContent = [
        instances.length ? `${instances.length} instance${instances.length === 1 ? '' : 's'}` : null,
        databases.length ? `${databases.length} database${databases.length === 1 ? '' : 's'}` : null,
    ].filter(Boolean).join(' · ');

    if (!instances.length && !databases.length) {
        list.innerHTML = `<div class="nd-empty"><strong>Nothing runs here yet</strong>Pick this node when you add an instance to an app, or when you create a database.</div>`;
        return;
    }

    const instanceRow = i => {
        const detail = i.last_error && i.status === 'error'
            ? `<span class="nd-sub error">${escHtml(i.last_error)}</span>`
            : `<span class="nd-sub">Instance ${i.id}${i.substatus ? ' · ' + escHtml(i.substatus.replace(/_/g, ' ')) : ''}</span>`;
        return `<tr class="clickable" data-href="/app?id=${i.app_id}">
            <td><span class="nd-name"><a href="/app?id=${i.app_id}">${escHtml(i.app_name)}</a></span>${detail}</td>
            <td><span class="nd-kind">Instance</span></td>
            <td>${badge(i.status)}</td>
            <td class="nd-addr">${i.address ? escHtml(i.address) : '—'}</td>
        </tr>`;
    };
    const databaseRow = d => `<tr class="clickable" data-href="/database?id=${d.id}">
            <td><span class="nd-name"><a href="/database?id=${d.id}">${escHtml(d.name)}</a></span><span class="nd-sub">${escHtml(d.engine)} ${escHtml(d.version)}</span></td>
            <td><span class="nd-kind">Database</span></td>
            <td>${badge(d.status)}</td>
            <td class="nd-addr">—</td>
        </tr>`;

    list.innerHTML = `<table class="table nd-run">
        <thead><tr><th>Name</th><th>Kind</th><th>Status</th><th>Reached at</th></tr></thead>
        <tbody>${instances.map(instanceRow).join('')}${databases.map(databaseRow).join('')}</tbody>
    </table>`;
    list.querySelectorAll('tr[data-href]').forEach(tr => tr.addEventListener('click', e => {
        if (!e.target.closest('a')) location.href = tr.dataset.href;
    }));
}

/* ─── This server ──────────────────────────────────────────────────────── */
// Values are escaped (most come from the node's own registration); html: true for trusted markup.
function _detailItem(label, val, { valueClass = '', html = false } = {}) {
    const classes = ['node-detail-value', valueClass].filter(Boolean).join(' ');
    return `<div class="node-detail-item"><span class="node-detail-label">${label}</span><span class="${classes}">${html ? val : escHtml(val)}</span></div>`;
}

function renderAbout() {
    const meta = node.metadata || {};
    const connected = node.is_local || node.websocket_connected;
    document.getElementById('node-info-grid').innerHTML = [
        meta.hostname ? _detailItem('Hostname', meta.hostname) : '',
        meta.ip ? _detailItem('IP address', meta.ip, { valueClass: 'node-detail-value-mono' }) : '',
        node.wg_ip ? _detailItem('Node network address', node.wg_ip, { valueClass: 'node-detail-value-mono' }) : '',
        meta.os ? _detailItem('Operating system', meta.os_short || meta.os) : '',
        meta.arch ? _detailItem('Architecture', meta.arch) : '',
        meta.cpu_model ? _detailItem('Processor', meta.cpu_model) : '',
        meta.cpu_count ? _detailItem('CPU cores', `${meta.cpu_count} physical / ${meta.cpu_count_logical || meta.cpu_count} logical`) : '',
        meta.ram_total_mb ? _detailItem('Memory', `${Math.round(meta.ram_total_mb / 1024)} GB`) : '',
        meta.disk_total_gb ? _detailItem('Storage', `${meta.disk_total_gb} GB`) : '',
        meta.uptime_secs ? _detailItem('Uptime', fmtUptime(meta.uptime_secs)) : '',
        _detailItem('Agent', node.agent_version || '—'),
        _detailItem('Last seen', connected ? 'Connected now' : timeAgo(node.last_seen), { valueClass: connected ? 'node-detail-value-live' : '' }),
        _detailItem('Node ID', `#${node.id}`, { valueClass: 'node-detail-value-mono' }),
    ].join('');
}

/* ─── Live updates ─────────────────────────────────────────────────────── */
function setupWebSockets() {
    if (node.is_local) return;
    wsNodeEvents(node.id, event => {
        if (event.type === 'node_health') {
            updateMetrics(event);
            if (node.status !== 'online') {
                node.status = 'online';
                _setChartsOfflineState(false);
                renderAll();
            }
        }
        if (event.type === 'node_offline') {
            node.status = 'offline';
            _setChartsOfflineState(true);
            renderAll();
        }
    });
}

function updateMetrics(m) {
    node.node_metrics = { ...(node.node_metrics || {}), ...pick(m, ['cpu_percent', 'memory_percent', 'disk_percent']) };
    renderTiles();
}

function pick(obj, keys) {
    return Object.fromEntries(keys.filter(k => obj[k] != null).map(k => [k, obj[k]]));
}

// ── Charts ────────────────────────────────────────────────────────────────────
const OFFLINE_SVG = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/></svg>`;

function buildCharts() {
    document.getElementById('nd-charts').innerHTML = CHARTS.map(c => `
        <div class="card chart-card">
            <div class="chart-hdr">
                <div>
                    <div class="chart-title">${c.title}</div>
                    <div class="chart-subtitle node-range-label">${RANGE_LABELS[_nodeRangeHours]}</div>
                    ${c.series.length > 1 ? `<div class="nd-legend">${c.series.map(x => `<span class="nd-key" style="--c:var(${x.color})">${x.label}</span>`).join('')}</div>` : ''}
                </div>
                <div class="nd-chart-tools">
                    <button type="button" class="btn btn-secondary btn-sm history-open-btn" data-chart="${c.id}" style="font-size:10px;padding:3px 8px">Open Large</button>
                    <span class="chart-badge">${c.unit}</span>
                </div>
            </div>
            <canvas id="chart-${c.id}" style="width:100%;height:110px;display:block"></canvas>
            <div id="chart-${c.id}-empty" class="chart-offline-overlay" style="display:none"></div>
        </div>`).join('');
    _setChartsOfflineState(node.status !== 'online' && !node.is_local);
    document.getElementById('nd-charts').addEventListener('click', e => {
        const btn = e.target.closest('[data-chart]');
        if (btn) openLargeChart(btn.dataset.chart);
    });
    initRange();
}

function initRange() {
    const group = document.getElementById('node-range');
    group.addEventListener('click', e => {
        const btn = e.target.closest('[data-hours]');
        if (!btn) return;
        _nodeRangeHours = Number(btn.dataset.hours);
        group.querySelectorAll('[data-hours]').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
        document.querySelectorAll('.node-range-label').forEach(el => { el.textContent = RANGE_LABELS[_nodeRangeHours]; });
        loadMetrics();
    });
}

// The server samples every node in the background and keeps 7 days, so the charts are full when the page opens.
async function loadMetrics() {
    const hours = _nodeRangeHours;
    try {
        const { samples = [] } = await api.getNodeMetricsHistory(NODE_ID, hours);
        if (hours !== _nodeRangeHours) return;   // another range was picked meanwhile
        metricSamples = samples;
        drawCharts();
        renderTiles();
    } catch { /* history is optional */ }
}

function drawCharts() {
    for (const c of CHARTS) drawChart(c);
}

/** A step of 1, 2 or 5 times a power of ten above the value: the top of a chart that is not a percentage. */
function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    return [1, 2, 5, 10].map(m => m * p).find(x => x >= v) || v;
}

/** Time labels along the bottom of a large chart: evenly spread, as many as fit. */
function timeLabel(t) {
    const d = new Date(t);
    return _nodeRangeHours > 24
        ? d.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
        : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function drawChart(chart, { canvas = document.getElementById(`chart-${chart.id}`), large = false } = {}) {
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = devicePixelRatio;
    canvas.width  = canvas.offsetWidth  * dpr;
    canvas.height = canvas.offsetHeight * dpr;
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const lines = chart.series.map(s => ({ ...s, pts: metricSamples.filter(x => x[s.key] != null).map(x => ({ t: x.ts, v: x[s.key] })) }));
    const all = lines.flatMap(l => l.pts);
    if (all.length < 2) {
        ctx.font = `${12 * dpr}px Geist, sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = cssVar('--faint');
        ctx.fillText('Collecting data\u2026', W / 2, H / 2);
        return;
    }

    const rawMax = Math.max(...all.map(p => p.v));
    const yMax = chart.pct ? Math.max(Math.ceil(rawMax / 10) * 10, 10) : niceMax(rawMax);
    const t0 = Math.min(...all.map(p => p.t)), t1 = Math.max(...all.map(p => p.t));
    const steps = large ? 4 : 2;                                  // grid lines: 0, half and top; the large one has quarters
    ctx.font = `${(large ? 10 : 9) * dpr}px Geist, sans-serif`;
    const labelWidth = Math.max(...Array.from({ length: steps + 1 }, (_, i) => ctx.measureText(chart.fmt(yMax * i / steps).replace('%', '')).width)) / dpr + 10;
    const pL = labelWidth * dpr, pR = (large ? 12 : 6) * dpr, pT = (large ? 24 : 16) * dpr, pB = (large ? 24 : 4) * dpr;
    const cW = W - pL - pR, cH = H - pT - pB;
    const x = t => pL + (t1 === t0 ? 0 : (t - t0) / (t1 - t0)) * cW;
    const y = v => pT + cH - Math.min(Math.max(v, 0) / yMax, 1) * cH;

    // Grid lines and labels
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= steps; i++) {
        const yy = pT + cH - (i / steps) * cH;
        ctx.strokeStyle = cssVar('--line');
        ctx.lineWidth = dpr;
        ctx.beginPath(); ctx.moveTo(pL, yy); ctx.lineTo(W - pR, yy); ctx.stroke();
        ctx.fillStyle = cssVar('--faint');
        ctx.fillText(chart.fmt(yMax * i / steps).replace('%', ''), pL - 4 * dpr, yy);
    }

    // Time along the bottom of the large chart
    if (large) {
        ctx.font = `${9.5 * dpr}px Geist, sans-serif`;
        ctx.textBaseline = 'top';
        ctx.fillStyle = cssVar('--faint');
        const labelPx = ctx.measureText(timeLabel(t0)).width + 28 * dpr;
        const count = Math.max(2, Math.min(10, Math.floor(cW / labelPx)));
        for (let i = 0; i < count; i++) {
            ctx.textAlign = i === 0 ? 'left' : i === count - 1 ? 'right' : 'center';
            ctx.fillText(timeLabel(t0 + (t1 - t0) * i / (count - 1)), pL + (cW * i) / (count - 1), pT + cH + 6 * dpr);
        }
    }

    lines.forEach((line, index) => {
        if (line.pts.length < 2) return;
        const color = cssVar(line.color);
        const stroke = () => {
            ctx.beginPath();
            line.pts.forEach((p, i) => (i === 0 ? ctx.moveTo(x(p.t), y(p.v)) : ctx.lineTo(x(p.t), y(p.v))));
        };
        if (index === 0 || lines.length === 1) {              // the first line gets the soft fill under it
            const grad = ctx.createLinearGradient(0, pT, 0, pT + cH);
            grad.addColorStop(0, color + '1c');
            grad.addColorStop(1, color + '00');
            stroke();
            ctx.lineTo(x(line.pts.at(-1).t), pT + cH);
            ctx.lineTo(x(line.pts[0].t), pT + cH);
            ctx.closePath();
            ctx.fillStyle = grad;
            ctx.fill();
        }
        stroke();
        ctx.strokeStyle = color;
        ctx.lineWidth = (large ? 2 : 1.5) * dpr;
        ctx.lineJoin = 'round';
        ctx.stroke();
    });

    // The latest value of a single line, top right
    if (lines.length === 1) {
        ctx.font = `600 ${(large ? 12 : 10) * dpr}px Geist, sans-serif`;
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillStyle = cssVar(lines[0].color);
        ctx.fillText(chart.fmt(lines[0].pts.at(-1).v), W - pR, 4 * dpr);
    }
}

/** The same chart in a big dialog, like "Open Large" on an app's metrics. */
function openLargeChart(id) {
    const chart = CHARTS.find(c => c.id === id);
    const hasData = chart && chart.series.some(s => metricSamples.filter(x => x[s.key] != null).length >= 2);
    if (!hasData) {
        toast('No history data to enlarge yet', 'error');
        return;
    }
    const legend = chart.series.length > 1
        ? chart.series.map(x => `<span class="nd-key" style="--c:var(${x.color})">${x.label}</span>`).join('')
        : '';
    const backdrop = document.createElement('div');
    backdrop.className = 'dialog-backdrop';
    backdrop.innerHTML = `
        <div class="dialog dialog-modern" style="max-width:min(1100px,96vw);width:min(1100px,96vw)">
            <div class="dialog-title">${chart.title}</div>
            <div class="dialog-body" style="padding:14px 16px 10px;display:flex;flex-direction:column;gap:8px">
                <div style="display:flex;gap:14px;align-items:center;font-size:12px;color:var(--muted)">
                    <span>${RANGE_LABELS[_nodeRangeHours]} \u00b7 ${chart.unit}</span><span class="nd-legend" style="margin:0">${legend}</span>
                </div>
                <div style="height:min(62vh,560px)"><canvas id="nd-large-canvas" style="width:100%;height:100%;display:block"></canvas></div>
            </div>
            <div class="dialog-actions"><button class="btn btn-secondary" id="nd-large-close">Close</button></div>
        </div>`;
    document.body.appendChild(backdrop);
    const canvas = backdrop.querySelector('#nd-large-canvas');
    const draw = () => drawChart(chart, { canvas, large: true });
    draw();
    window.addEventListener('resize', draw, { passive: true });
    const close = () => {
        window.removeEventListener('resize', draw);
        document.removeEventListener('keydown', onKey);
        backdrop.remove();
    };
    const onKey = e => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    backdrop.querySelector('#nd-large-close').onclick = close;
    backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
}

function _setChartsOfflineState(offline) {
    for (const c of CHARTS) {
        const canvas = document.getElementById(`chart-${c.id}`);
        const overlay = document.getElementById(`chart-${c.id}-empty`);
        if (!overlay) continue;
        if (offline) {
            overlay.innerHTML = `${OFFLINE_SVG}<span>Node offline</span>`;
            overlay.style.display = 'flex';
            if (canvas) canvas.style.opacity = '0';
        } else {
            overlay.style.display = 'none';
            if (canvas) canvas.style.opacity = '1';
        }
    }

    const logsOverlay = document.getElementById('node-logs-empty');
    const logsContent = document.getElementById('node-logs-content');
    if (logsOverlay) {
        if (offline) {
            logsOverlay.innerHTML = `${OFFLINE_SVG}<span>Node offline</span>`;
            logsOverlay.style.display = 'flex';
            if (logsContent) logsContent.style.opacity = '0';
            hasLoadedLogs = false;
            lastLogsText = '';
        } else {
            logsOverlay.style.display = 'none';
            if (logsContent) logsContent.style.opacity = '1';
        }
    }
}

/* ─── Agent log and commands ───────────────────────────────────────────── */
function initTabs() {
    const group = document.getElementById('nd-tabs');
    group.addEventListener('click', e => {
        const btn = e.target.closest('[data-tab]');
        if (!btn) return;
        activeTab = btn.dataset.tab;
        group.querySelectorAll('[data-tab]').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
        document.getElementById('tab-log').hidden = activeTab !== 'log';
        document.getElementById('tab-commands').hidden = activeTab !== 'commands';
        if (activeTab === 'commands') loadHistory();
        else loadLogs();
    });
}

async function loadLogs() {
    const content = document.getElementById('node-logs-content');
    if (!content) return;
    if (!node || (node.status !== 'online' && !node.is_local)) {
        _setChartsOfflineState(true);
        return;
    }
    const requestId = ++latestLogsRequest;
    const shouldAutoScroll = !hasLoadedLogs || _isNearBottom(content);
    const prevScrollTop = content.scrollTop;
    const prevScrollHeight = content.scrollHeight;

    if (!hasLoadedLogs && !content.textContent.trim()) {
        content.textContent = 'Loading…';
    }
    try {
        const r = await api.getNodeAgentLogs(NODE_ID, 500);
        if (requestId !== latestLogsRequest) return;

        const nextText = r.lines.join('\n') || '(no logs found)';
        if (!hasLoadedLogs || nextText !== lastLogsText) {
            content.textContent = nextText;
            lastLogsText = nextText;

            if (shouldAutoScroll) {
                content.scrollTop = content.scrollHeight;
            } else {
                const nextScrollHeight = content.scrollHeight;
                content.scrollTop = prevScrollTop + Math.max(0, nextScrollHeight - prevScrollHeight);
            }
        }
        hasLoadedLogs = true;
    } catch (e) {
        if (requestId !== latestLogsRequest) return;
        content.textContent = `Error: ${e.message}`;
        hasLoadedLogs = false;
        lastLogsText = '';
    }
}

function _isNearBottom(el, threshold = 28) {
    return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}

async function loadHistory() {
    const list = document.getElementById('node-history-list');
    if (!list) return;
    if (!list.children.length) list.innerHTML = `<span style="color:var(--muted)">${spinner} Loading…</span>`;
    try {
        const history = await api.listNodeCommands(NODE_ID);
        if (!history.length) {
            list.innerHTML = `<span style="color:var(--muted)">No commands yet. Questions and jobs the primary sends this node (logs, stats, backups) show up here.</span>`;
            return;
        }
        list.innerHTML = history.slice(0, 80).map(c => {
            const tone = c.status === 'done' ? 'var(--green)' : (c.status === 'failed' ? 'var(--red)' : 'var(--yellow)');
            return `<div class="cmd-row">
                <span class="dot" style="background:${tone}"></span>
                <span class="cmd-row-time">${fmtDate(c.created_at)}</span>
                <span class="cmd-row-type">${escHtml(c.command_type)}${c.app_id ? ' \u00b7 app #' + c.app_id : ''}</span>
                <span class="cmd-row-status" style="color:${tone}">${escHtml(c.status)}</span>
                ${c.error_message ? `<span class="cmd-row-error">${escHtml(c.error_message)}</span>` : ''}
            </div>`;
        }).join('');
    } catch (e) {
        list.innerHTML = `<span style="color:var(--red)">${escHtml(e.message)}</span>`;
    }
}

/* ─── Actions ───────────────────────────────────────────────────────────── */
async function cleanupDocker() {
    const btn = document.getElementById('btn-docker-cleanup');
    btn.disabled = true;
    btn.innerHTML = `${spinner} Cleaning…`;
    try {
        const r = await api.cleanupNodeDocker(NODE_ID);
        if (r.skipped) toast(r.skipped, 'error');
        else if (r.errors && r.errors.length && !r.summary) toast(r.errors[0], 'error');
        else toast(r.summary || 'Cleanup done');
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Clean up Docker';
    }
}

async function renameNode() {
    const newName = await prompt('Rename Node', 'New display name for this node', node.name);
    if (!newName || newName === node.name) return;

    try {
        const updated = await api.updateNode(NODE_ID, { name: newName.trim() });
        node.name = updated.name;
        renderHeader();
        toast('Node name updated');
    } catch (e) {
        toast(e.message, 'error');
    }
}

async function toggleEnable() {
    const btn = document.getElementById('btn-toggle-enable');
    btn.disabled = true;
    try {
        if (node.enabled) await api.disableNode(NODE_ID);
        else              await api.enableNode(NODE_ID);
        node.enabled = !node.enabled;
        renderHeader();
        toast(`Node ${node.enabled ? 'enabled' : 'disabled'}`);
    } catch (e) {
        toast(e.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

async function deleteNode() {
    const ok = await confirm(`Remove node "${node.name}"?`, 'This cannot be undone.');
    if (!ok) return;
    try {
        await api.deleteNode(NODE_ID);
        toast('Node removed');
        window.location.href = '/';
    } catch (e) {
        toast(e.message, 'error');
    }
}

function escHtml(v) {
    return String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
