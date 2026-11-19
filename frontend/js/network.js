// The network page: how traffic comes in at the primary and reaches every node, and the numbers of each link.
import { api } from './api.js';
import { esc } from './shell.js';
import { fmtUptime, timeAgo } from './utils.js';

const REFRESH_MS = 5000;
const PING_EVERY_MS = 5000;

let nodes = [];
let mesh = null;
const pings = new Map();          // node id → { agent, wg } in ms (null: no answer)
let lastPingRound = 0;
let pinging = false;              // a round is under way: the next one waits for it

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
const ms = v => v == null ? null : v < 10 ? `${v.toFixed(1)} ms` : `${Math.round(v)} ms`;

async function measureLatency() {
  if (pinging) return;
  pinging = true;
  try {
    await pingRound();
  } finally {
    pinging = false;
    lastPingRound = Date.now();
  }
  render();
}

async function pingRound() {
  const remote = nodes.filter(n => !n.is_local && n.enabled && n.status === 'online');
  await Promise.all(remote.map(async n => {
    try {
      const r = await api.pingNode(n.id);
      pings.set(n.id, { agent: r.reachable ? r.latency_ms : null, wg: r.wg_latency_ms ?? null });
    } catch {
      pings.delete(n.id);
    }
  }));
}

/* ─── Numbers ──────────────────────────────────────────────────────────── */
function renderKpis() {
  const remote = nodes.filter(n => !n.is_local);
  const online = remote.filter(n => n.enabled && n.status === 'online').length;
  const linked = remote.filter(n => linkOf(n).key === 'up').length;
  let rx = 0, tx = 0, any = false;
  for (const n of nodes) if (n.traffic) { rx += n.traffic.in; tx += n.traffic.out; any = true; }
  const wg = [...pings.values()].map(p => p.wg).filter(v => v != null);
  const hub = mesh ? (!mesh.enabled ? 'Off' : mesh.up ? 'Up' : 'Down') : '—';
  const hubSub = mesh && mesh.enabled ? `${esc(mesh.subnet)} · UDP ${mesh.port}` : 'Node network is switched off';
  const kpi = (label, value, sub) =>
    `<div class="kpi"><span class="kpi-label">${label}</span><span class="kpi-value"><strong>${value}</strong></span><span class="kpi-sub">${sub}</span></div>`;
  document.getElementById('net-kpis').innerHTML = [
    kpi('Nodes online', `${online}<span> / ${remote.length}</span>`, remote.length ? `${linked} on the node network` : 'Only the primary so far'),
    kpi('Node network', hub, hubSub),
    kpi('WireGuard latency', wg.length ? ms(wg.reduce((a, b) => a + b, 0) / wg.length) : '—', wg.length ? `average · slowest ${ms(Math.max(...wg))}` : 'No measurement yet'),
    kpi('Traffic in', any ? speed(rx) : '—', 'All servers together'),
    kpi('Traffic out', any ? speed(tx) : '—', 'All servers together'),
  ].join('');
}

/* ─── Map ──────────────────────────────────────────────────────────────── */
// A tree from top to bottom, the way traffic flows: the internet, the primary (where everything comes in), the node
// network, and the nodes. Cards are HTML so they wrap on narrow screens; the wires are drawn afterwards between them.

const ICON = {
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z"/>',
  server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
};
const icon = (name, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

const pct = v => v == null ? null : Math.max(0, Math.min(100, Math.round(v)));
const load = (label, v) => v == null ? '' : `<span class="${v >= 90 ? 'bad' : v >= 75 ? 'warn' : ''}">${label} ${pct(v)}%</span>`;

function primaryCard(p) {
  const m = (p && p.node_metrics) || {};
  const t = p && p.traffic;
  return `
    <a class="topo-primary" id="topo-primary" href="${p ? `/node?id=${p.id}` : '#'}" title="The primary: panel, nginx and the node network hub">
      <span class="topo-icon topo-icon--accent">${icon('server', 15)}</span>
      <div class="topo-title">
        <strong>${esc(p ? p.name : 'Primary')}<em>entry point</em></strong>
        <span class="mono">${esc(location.host)}</span>
      </div>
      <div class="topo-traffic-big"><span>↓ ${esc(speed(t && t.in))}</span><span>↑ ${esc(speed(t && t.out))}</span></div>
      <div class="topo-load">${load('cpu', m.cpu_percent)}${load('mem', m.memory_percent)}</div>
    </a>`;
}

function nodeCard(n) {
  const link = linkOf(n);
  const m = n.node_metrics || {};
  const ping = pings.get(n.id);
  const t = n.traffic;
  const latency = link.key !== 'up' ? '' : !ping ? '…' : ping.wg != null ? ms(ping.wg) : 'no answer';
  const tip = [n.name, link.label, n.wg_ip, n.status === 'online' && n.online_since ? `online ${sinceOnline(n)}` : null,
    n.status !== 'online' ? `last seen ${timeAgo(n.last_seen)}` : null].filter(Boolean).join(' · ');
  return `
    <a class="topo-node ${link.key}" href="/node?id=${n.id}" title="${esc(tip)}">
      <div class="topo-node-head">
        <i class="net-dot ${link.key}"></i>
        <strong>${esc(n.name)}</strong>
        ${latency ? `<span class="topo-latency">${esc(latency)}</span>` : `<span class="topo-node-off">${esc(link.label)}</span>`}
      </div>
      <div class="topo-node-foot">
        <span class="topo-node-traffic">↓ ${esc(speed(t && t.in))}  ↑ ${esc(speed(t && t.out))}</span>
        ${n.status === 'online' ? `<span class="topo-load">${load('cpu', m.cpu_percent)}${load('mem', m.memory_percent)}</span>` : ''}
      </div>
    </a>`;
}

function renderMap() {
  const primary = nodes.find(n => n.is_local);
  const remote = nodes.filter(n => !n.is_local);
  const meshOn = mesh && mesh.enabled;
  const meshTone = meshOn && mesh.up ? 'up' : meshOn ? 'down' : 'idle';
  const linked = remote.filter(n => linkOf(n).key === 'up').length;

  document.getElementById('net-map').innerHTML = `
    <div class="topo" id="topo">
      <svg class="topo-wires" id="topo-wires" aria-hidden="true"></svg>
      <div class="topo-tier">
        <div class="topo-internet" id="topo-internet">
          ${icon('globe', 14)}
          <strong>Internet</strong>
        </div>
      </div>
      <div class="topo-tier"><span class="topo-wire-label">HTTPS · 80 / 443</span></div>
      <div class="topo-tier">${primaryCard(primary)}</div>
      ${remote.length ? `
        <div class="topo-tier"><span class="topo-wire-label ${meshTone}">${icon('shield', 13)}WireGuard${meshOn ? ` · UDP ${mesh.port}` : ' · off'}</span></div>
        <section class="topo-group ${meshTone}" id="topo-group" aria-label="Node network">
          <div class="topo-group-head">
            <strong>Node network</strong>
            <span class="mono">${meshOn ? esc(mesh.subnet) : 'switched off'}</span>
            <span class="topo-group-count">${linked} / ${remote.length} connected</span>
          </div>
          <div class="topo-nodes">${remote.map(nodeCard).join('')}</div>
        </section>` : ''}
    </div>`;
  document.getElementById('net-map-note').textContent = remote.length ? 'All traffic comes in at the primary' : '';
  drawWires();
}

/** The wires between the parts, measured from where the browser put them. */
function drawWires() {
  const topo = document.getElementById('topo');
  const svg = document.getElementById('topo-wires');
  if (!topo || !svg) return;
  const box = topo.getBoundingClientRect();
  const at = (el, side) => {
    const r = el.getBoundingClientRect();
    return { x: r.left - box.left + r.width / 2, y: (side === 'top' ? r.top : r.bottom) - box.top };
  };
  const wire = (a, b, cls) => `<path class="wire ${cls}" d="M${a.x},${a.y} L${b.x},${b.y}"/>`;
  const internet = document.getElementById('topo-internet');
  const primary = document.getElementById('topo-primary');
  const group = document.getElementById('topo-group');
  const paths = [];
  if (internet && primary) paths.push(wire(at(internet, 'bottom'), at(primary, 'top'), 'up flow'));
  if (primary && group) {
    const tone = mesh && mesh.enabled && mesh.up ? 'up flow' : mesh && mesh.enabled ? 'down' : 'idle';
    paths.push(wire(at(primary, 'bottom'), at(group, 'top'), tone));
  }
  svg.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  svg.setAttribute('width', box.width);
  svg.setAttribute('height', box.height);
  svg.innerHTML = paths.join('');
}

/* ─── Table ────────────────────────────────────────────────────────────── */
function renderTable() {
  const remote = nodes.filter(n => !n.is_local);
  const wrap = document.getElementById('net-table');
  if (!remote.length) {
    wrap.innerHTML = '<div class="apps-empty">No nodes yet.</div>';
    return;
  }
  const rows = remote.map(n => {
    const link = linkOf(n);
    const ping = pings.get(n.id);
    const wg = !ping ? '—' : ping.wg != null ? ms(ping.wg) : 'no answer';
    const agent = !ping ? '—' : ping.agent != null ? ms(ping.agent) : 'no answer';
    const t = n.traffic;
    const m = n.node_metrics || {};
    return `<tr>
      <td class="net-who"><a href="/node?id=${n.id}">${esc(n.name)}</a></td>
      <td><span class="net-state"><i class="net-dot ${link.key}"></i>${esc(link.label)}</span></td>
      <td class="net-mono">${esc(n.wg_ip || '—')}</td>
      <td>${n.status === 'online' ? esc(sinceOnline(n)) : `<span class="net-note">last seen ${esc(timeAgo(n.last_seen))}</span>`}</td>
      <td>${n.wg_handshake_at ? esc(timeAgo(n.wg_handshake_at)) : '—'}</td>
      <td class="net-mono">${esc(wg)}</td>
      <td class="net-mono">${esc(agent)}</td>
      <td class="net-mono">${esc(speed(t && t.in))}</td>
      <td class="net-mono">${esc(speed(t && t.out))}</td>
      <td class="net-mono">${m.cpu_percent != null ? `${Math.round(m.cpu_percent)}% cpu · ${Math.round(m.memory_percent)}% mem` : '—'}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `<table class="table net-table">
    <thead><tr><th>Node</th><th>Link</th><th>Address</th><th>Online for</th><th>Handshake</th>
      <th title="Round trip from the primary to the node over WireGuard">WireGuard</th>
      <th title="Round trip of a message to the agent and back, through the panel connection">Agent</th>
      <th>In</th><th>Out</th><th>Load</th></tr></thead>
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
    render();
    if (!pinging && Date.now() - lastPingRound >= PING_EVERY_MS - 500) measureLatency();
  } catch (e) {
    document.getElementById('net-map').innerHTML = `<div class="apps-empty">${esc(e.message)}</div>`;
  }
}

export function initNetwork() {
  refresh();
  setInterval(refresh, REFRESH_MS);
  // Cards move when the window changes width (nodes wrap to a new row): the wires follow
  const map = document.getElementById('net-map');
  if (map && 'ResizeObserver' in window) new ResizeObserver(() => drawWires()).observe(map);
}
