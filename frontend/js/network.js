// The network page: the primary in the middle, every node on a line to it, and the numbers of each link.
import { api } from './api.js';
import { esc } from './shell.js';
import { fmtUptime, timeAgo } from './utils.js';

const REFRESH_MS = 5000;
const PING_EVERY_MS = 20000;

let nodes = [];
let mesh = null;
const lastCounters = new Map();   // node id → { rx, tx, at }
const rates = new Map();          // node id → { rx, tx } in bytes per second
const pings = new Map();          // node id → { ms } | { unreachable: true }
let lastPingRound = 0;

/** The state of the link between a node and the primary. */
function linkOf(n) {
  if (n.is_local) return { key: 'up', label: 'This server' };
  if (!n.enabled) return { key: 'idle', label: 'Disabled' };
  if (n.status !== 'online') return { key: 'down', label: 'Offline' };
  if (n.wg_state === 'up') return { key: 'up', label: 'Connected' };
  if (n.wg_state === 'down') return { key: 'down', label: 'Network down' };
  return { key: 'idle', label: 'Not on the node network' };
}

const sinceOnline = n => n.online_since ? fmtUptime(Math.max(0, Math.floor((Date.now() - new Date(n.online_since).getTime()) / 1000))) : '—';

function bytes(b) {
  if (b == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  return `${b < 10 && i > 0 ? b.toFixed(1) : Math.round(b)} ${units[i]}`;
}
const speed = r => r == null ? '—' : `${bytes(r)}/s`;

function updateRates() {
  const now = Date.now();
  for (const n of nodes) {
    if (n.net_rx_bytes == null || n.net_tx_bytes == null) { rates.delete(n.id); continue; }
    const prev = lastCounters.get(n.id);
    if (prev && now > prev.at && n.net_rx_bytes >= prev.rx && n.net_tx_bytes >= prev.tx) {
      const s = (now - prev.at) / 1000;
      rates.set(n.id, { rx: (n.net_rx_bytes - prev.rx) / s, tx: (n.net_tx_bytes - prev.tx) / s });
    } else {
      rates.delete(n.id);                       // first reading, or the counters restarted
    }
    lastCounters.set(n.id, { rx: n.net_rx_bytes, tx: n.net_tx_bytes, at: now });
  }
}

async function measureLatency() {
  const remote = nodes.filter(n => !n.is_local && n.enabled && n.status === 'online' && n.wg_state === 'up');
  await Promise.all(remote.map(async n => {
    try {
      const r = await api.pingNode(n.id);
      pings.set(n.id, r.reachable ? { ms: r.latency_ms } : { unreachable: true });
    } catch {
      pings.delete(n.id);
    }
  }));
  lastPingRound = Date.now();
  render();
}

/* ─── Numbers ──────────────────────────────────────────────────────────── */
function renderKpis() {
  const remote = nodes.filter(n => !n.is_local);
  const online = remote.filter(n => n.enabled && n.status === 'online').length;
  const linked = remote.filter(n => linkOf(n).key === 'up').length;
  let rx = 0, tx = 0, any = false;
  for (const r of rates.values()) { rx += r.rx; tx += r.tx; any = true; }
  const hub = mesh
    ? (!mesh.enabled ? 'Off' : mesh.up ? 'Up' : 'Down')
    : '—';
  const hubSub = mesh && mesh.enabled ? `${esc(mesh.subnet)} · UDP ${mesh.port}` : 'Node network is switched off';
  const kpi = (label, value, sub) =>
    `<div class="kpi"><span class="kpi-label">${label}</span><span class="kpi-value"><strong>${value}</strong></span><span class="kpi-sub">${sub}</span></div>`;
  document.getElementById('net-kpis').innerHTML = [
    kpi('Nodes online', `${online}<span> / ${remote.length}</span>`, remote.length ? `${linked} on the node network` : 'Only the primary so far'),
    kpi('Node network', hub, hubSub),
    kpi('Traffic in', any ? speed(rx) : '—', 'All nodes together'),
    kpi('Traffic out', any ? speed(tx) : '—', 'All nodes together'),
  ].join('');
}

/* ─── Map ──────────────────────────────────────────────────────────────── */
const W = 880, H = 440, CX = W / 2, CY = H / 2;
const BOX = { w: 168, h: 46 };

function place(count, i) {
  if (count === 1) return { x: CX + 280, y: CY };
  const a = (-Math.PI / 2) + (2 * Math.PI * i) / count;
  return { x: CX + Math.cos(a) * 320, y: CY + Math.sin(a) * 160 };
}

function boxSvg(x, y, title, sub, state, href, cls = '') {
  const inner = `
    <g class="net-box-wrap ${cls}" transform="translate(${(x - BOX.w / 2).toFixed(1)},${(y - BOX.h / 2).toFixed(1)})">
      <rect class="net-box" width="${BOX.w}" height="${BOX.h}" rx="9"/>
      <circle class="net-status ${state}" cx="16" cy="${BOX.h / 2}" r="4"/>
      <text class="net-name" x="30" y="19">${esc(title.length > 18 ? title.slice(0, 17) + '…' : title)}</text>
      <text class="net-sub" x="30" y="35">${esc(sub)}</text>
    </g>`;
  return href ? `<a href="${href}">${inner}</a>` : inner;
}

function renderMap() {
  const hubNode = nodes.find(n => n.is_local);
  const remote = nodes.filter(n => !n.is_local);
  const lines = [], boxes = [];

  remote.forEach((n, i) => {
    const p = place(remote.length, i);
    const link = linkOf(n);
    lines.push(`<line class="net-line ${link.key}" x1="${CX}" y1="${CY}" x2="${p.x.toFixed(1)}" y2="${p.y.toFixed(1)}"/>`);
    const ping = pings.get(n.id);
    const label = link.key === 'up' && ping && ping.ms != null ? `${ping.ms} ms` : '';
    if (label) {
      lines.push(`<text class="net-edge-label" text-anchor="middle" x="${((CX + p.x) / 2).toFixed(1)}" y="${((CY + p.y) / 2 - 6).toFixed(1)}">${esc(label)}</text>`);
    }
    boxes.push(boxSvg(p.x, p.y, n.name, n.wg_ip || n.public_host || link.label, link.key, `/node?id=${n.id}`, 'net-node'));
  });
  boxes.push(boxSvg(CX, CY, hubNode ? hubNode.name : 'Primary', mesh && mesh.enabled ? `hub ${mesh.subnet}` : 'primary', 'up',
    hubNode ? `/node?id=${hubNode.id}` : null, 'net-hub'));

  document.getElementById('net-map').innerHTML =
    `<svg class="net-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="The primary and its nodes">${lines.join('')}${boxes.join('')}</svg>`;
  document.getElementById('net-map-note').textContent = remote.length ? '' : 'Add a node to see it here';
}

/* ─── Table ────────────────────────────────────────────────────────────── */
function renderTable() {
  const remote = nodes.filter(n => !n.is_local);
  const wrap = document.getElementById('net-table');
  if (!remote.length) {
    wrap.innerHTML = '<div class="apps-empty">No nodes yet. Add one from the overview to see its link here.</div>';
    return;
  }
  const rows = remote.map(n => {
    const link = linkOf(n);
    const ping = pings.get(n.id);
    const latency = ping ? (ping.ms != null ? `${ping.ms} ms` : 'not answering') : '—';
    const r = rates.get(n.id);
    const m = n.node_metrics || {};
    return `<tr>
      <td class="net-who"><a href="/node?id=${n.id}">${esc(n.name)}</a></td>
      <td><span class="net-state"><i class="net-dot ${link.key}"></i>${esc(link.label)}</span></td>
      <td class="net-mono">${esc(n.wg_ip || '—')}</td>
      <td>${n.status === 'online' ? esc(sinceOnline(n)) : `<span class="net-note">last seen ${esc(timeAgo(n.last_seen))}</span>`}</td>
      <td>${n.wg_handshake_at ? esc(timeAgo(n.wg_handshake_at)) : '—'}</td>
      <td class="net-mono">${esc(latency)}</td>
      <td class="net-mono">${esc(speed(r && r.rx))}</td>
      <td class="net-mono">${esc(speed(r && r.tx))}</td>
      <td class="net-mono">${m.cpu_percent != null ? `${Math.round(m.cpu_percent)}% cpu · ${Math.round(m.memory_percent)}% mem` : '—'}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table class="table net-table">
    <thead><tr><th>Node</th><th>Link</th><th>Address</th><th>Online for</th><th>Handshake</th><th>Latency</th><th>In</th><th>Out</th><th>Load</th></tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function render() {
  renderKpis();
  renderMap();
  renderTable();
}

async function refresh() {
  try {
    const [list, info] = await Promise.all([api.listNodes(), api.getMesh().catch(() => null)]);
    nodes = list;
    mesh = info;
    updateRates();
    render();
    if (Date.now() - lastPingRound > PING_EVERY_MS) { lastPingRound = Date.now(); measureLatency(); }
  } catch (e) {
    document.getElementById('net-map').innerHTML = `<div class="apps-empty">${esc(e.message)}</div>`;
  }
}

export function initNetwork() {
  refresh();
  setInterval(refresh, REFRESH_MS);
}
