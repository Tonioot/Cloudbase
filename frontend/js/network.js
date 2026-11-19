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
};
const icon = (name, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

const pct = v => v == null ? null : Math.max(0, Math.min(100, Math.round(v)));

function primaryCard(p) {
  const m = (p && p.node_metrics) || {};
  const t = p && p.traffic;
  const stat = (label, value, tone = '') => `<div class="tp-stat ${tone}"><em>${label}</em><b>${esc(value)}</b></div>`;
  const tone = v => v >= 90 ? 'bad' : v >= 75 ? 'warn' : '';
  return `
    <button type="button" class="topo-primary${p && p.id === selectedId ? ' selected' : ''}" id="topo-primary" data-select="${p ? p.id : ''}" aria-pressed="${p && p.id === selectedId}">
      <div class="tp-id">
        <span class="tp-kicker">Primary · entry point</span>
        <strong>${esc(p ? p.name : 'Primary')}</strong>
        <span class="tp-host">${esc(location.host)}</span>
      </div>
      <div class="tp-stats">
        ${stat('In', speed(t && t.in))}
        ${stat('Out', speed(t && t.out))}
        ${m.cpu_percent != null ? stat('CPU', `${pct(m.cpu_percent)}%`, tone(m.cpu_percent)) : ''}
        ${m.memory_percent != null ? stat('Memory', `${pct(m.memory_percent)}%`, tone(m.memory_percent)) : ''}
      </div>
    </button>`;
}

function nodeCard(n) {
  const link = linkOf(n);
  const m = n.node_metrics || {};
  const ping = pings.get(n.id);
  const t = n.traffic;
  const latency = link.key !== 'up' ? '' : !ping ? '' : ping.wg != null ? ms(ping.wg) : 'no reply';
  const tip = [n.name, link.label, n.wg_ip, n.status === 'online' && n.online_since ? `online ${sinceOnline(n)}` : null,
    n.status !== 'online' ? `last seen ${timeAgo(n.last_seen)}` : null,
    m.cpu_percent != null ? `cpu ${pct(m.cpu_percent)}% · memory ${pct(m.memory_percent)}%` : null].filter(Boolean).join(' · ');
  // A problem replaces the traffic line: what is wrong is what matters then
  let issue = '';
  if (link.key === 'down') issue = n.status !== 'online' ? `Offline · last seen ${timeAgo(n.last_seen)}` : 'Tunnel down';
  else if (link.key === 'idle') issue = link.label === 'Disabled' ? 'Disabled' : 'No tunnel to the primary';
  // A server under load says so where the latency normally is (one line, so every tile stays the same height)
  const hot = m.cpu_percent >= 85 ? `CPU ${pct(m.cpu_percent)}%` : m.memory_percent >= 85 ? `Memory ${pct(m.memory_percent)}%` : '';
  return `
    <button type="button" class="topo-node ${link.key}${n.id === selectedId ? ' selected' : ''}" data-select="${n.id}" aria-pressed="${n.id === selectedId}" title="${esc(tip)}">
      <div class="tn-head">
        <i class="tn-dot"></i>
        <strong>${esc(n.name)}</strong>
        ${hot && !issue ? `<span class="tn-hot">${esc(hot)}</span>` : latency ? `<span class="tn-lat">${esc(latency)}</span>` : ''}
      </div>
      ${issue
        ? `<div class="tn-issue">${esc(issue)}</div>`
        : `<div class="tn-stats"><span><em>in</em>${esc(speed(t && t.in))}</span><span><em>out</em>${esc(speed(t && t.out))}</span></div>`}
    </button>`;
}

/** Below this width the nodes go in one column with the trunk beside it; above it, half on each side of the trunk. */
const SPLIT_FROM_PX = 640;
let splitMode = null;

function splitNodes(remote) {
  const width = document.getElementById('net-map')?.clientWidth || 0;
  splitMode = width >= SPLIT_FROM_PX;
  let left = [], right = remote;
  if (splitMode) {
    // How many tiles fit on one side (see .topo-split / .topo-side in network.css)
    const side = (width - 32 - 36) / 2;
    const cols = Math.max(1, Math.floor((side + 10) / (168 + 10)));
    left = []; right = [];
    remote.forEach((n, i) => ((i % (2 * cols)) < cols ? left : right).push(n));
  }
  return `
    <div class="topo-split${splitMode ? '' : ' single'}" id="topo-split">
      ${splitMode ? `<div class="topo-side left">${left.map(nodeCard).join('')}</div>` : ''}
      <div class="topo-channel" id="topo-channel"></div>
      <div class="topo-side right">${right.map(nodeCard).join('')}</div>
    </div>`;
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
        <div class="topo-internet" id="topo-internet">${icon('globe', 13)}Internet</div>
      </div>
      <div class="topo-tier"><span class="topo-wire-label">HTTPS</span></div>
      <div class="topo-tier">${primaryCard(primary)}</div>
      ${remote.length ? `
        <div class="topo-tier">
          <span class="topo-hub ${meshTone}" id="topo-hub">WireGuard${meshOn ? ` <span>${esc(mesh.subnet)}</span>` : ' <span>off</span>'}<b>${linked} of ${remote.length} connected</b></span>
        </div>
        ${splitNodes(remote)}` : ''}
    </div>`;
  document.getElementById('net-map-note').textContent = remote.length ? 'Every node has its own tunnel to the primary' : '';
  drawWires();
  requestAnimationFrame(drawWires);              // once the browser has laid the new cards out
}

/** The wires, measured from where the browser put the parts. Every node has its own tunnel to the primary: the
 *  trunk runs from the hub straight down a channel between the nodes, branches left and right above each row, and a
 *  short line drops to each node in the colour of that node's tunnel. */
function drawWires() {
  const topo = document.getElementById('topo');
  const svg = document.getElementById('topo-wires');
  if (!topo || !svg) return;
  const box = topo.getBoundingClientRect();
  const rect = el => {
    const r = el.getBoundingClientRect();
    return { l: r.left - box.left, r: r.right - box.left, t: r.top - box.top, b: r.bottom - box.top, cx: r.left - box.left + r.width / 2 };
  };
  const R = 8;                                            // radius of every bend
  const f = n => n.toFixed(1);
  // A line through points, with rounded bends
  const path = (pts, cls) => {
    let d = `M${f(pts[0][0])},${f(pts[0][1])}`;
    for (let i = 1; i < pts.length; i++) {
      const [x, y] = pts[i];
      const next = pts[i + 1];
      if (!next) { d += ` L${f(x)},${f(y)}`; break; }
      const [px, py] = pts[i - 1];
      const r = Math.min(R, Math.hypot(x - px, y - py) / 2, Math.hypot(next[0] - x, next[1] - y) / 2);
      const ix = x - Math.sign(x - px) * r, iy = y - Math.sign(y - py) * r;
      const ox = x + Math.sign(next[0] - x) * r, oy = y + Math.sign(next[1] - y) * r;
      d += ` L${f(ix)},${f(iy)} Q${f(x)},${f(y)} ${f(ox)},${f(oy)}`;
    }
    return `<path class="wire ${cls}" d="${d}"/>`;
  };
  const stateOf = el => ['up', 'down', 'idle'].find(k => el.classList.contains(k)) || 'idle';
  const under = [], over = [];
  const internet = document.getElementById('topo-internet');
  const primary = document.getElementById('topo-primary');
  const hub = document.getElementById('topo-hub');
  const channel = document.getElementById('topo-channel');
  const meshUp = mesh && mesh.enabled && mesh.up;

  if (internet && primary) {
    const a = rect(internet), p = rect(primary);
    under.push(path([[a.cx, a.b], [p.cx, p.t]], 'trunk'));
  }
  if (primary && hub && channel) {
    const p = rect(primary), h = rect(hub), c = rect(channel);
    const trunk = meshUp ? 'trunk' : 'trunk off';
    const x = c.cx;
    under.push(path([[p.cx, p.b], [h.cx, h.t]], trunk));

    // The tiles by row, top to bottom; each row branches off the trunk to the left and to the right
    const rows = new Map();
    for (const tile of document.querySelectorAll('#topo-split .topo-node')) {
      const t = rect(tile);
      const key = Math.round(t.t);
      if (!rows.has(key)) rows.set(key, []);
      rows.get(key).push({ state: stateOf(tile), ...t });
    }
    const ordered = [...rows.entries()].sort((a, b) => a[0] - b[0]).map(([, tiles]) => tiles);
    if (ordered.length && !splitMode) {
      const tiles = ordered.map(row => row[0]);
      const lastMid = tiles[tiles.length - 1].t + (tiles[tiles.length - 1].b - tiles[tiles.length - 1].t) / 2;
      const down = [[h.cx, h.b], [h.cx, h.b + 10], [x, h.b + 10], [x, lastMid - 2 * R]];
      under.push(path(down, trunk));
      tiles.forEach((t, i) => {
        const mid = t.t + (t.b - t.t) / 2;
        const last = i === tiles.length - 1;
        over.push(path(last ? [[x, mid - 2 * R], [x, mid], [t.l, mid]] : [[x, mid], [t.l, mid]], `drop ${t.state}`));
      });
      if (tiles.length > 1) under.push(path([[x, lastMid - 2 * R], [x, tiles[tiles.length - 2].t]], trunk));
    } else if (ordered.length) {
      const busY = ordered.map(tiles => tiles[0].t - 17);
      const lastY = busY[busY.length - 1];

      // The trunk: from the hub into the channel and down to the last row (the hub may sit off the channel's line)
      const down = Math.abs(h.cx - x) < 1
        ? [[h.cx, h.b], [x, lastY]]
        : [[h.cx, h.b], [h.cx, h.b + 12], [x, h.b + 12], [x, lastY]];
      under.push(path(down, trunk));

      ordered.forEach((tiles, i) => {
        const y = busY[i];
        const last = i === ordered.length - 1;
        for (const side of [-1, 1]) {
          const here = tiles.filter(t => Math.sign(t.cx - x) === side).sort((a, b) => side * (a.cx - b.cx));
          if (!here.length) continue;
          const outer = here[here.length - 1];
          const reach = outer.cx - side * R * 1.6;             // the neutral branch stops where the outer node's bend starts
          // On the last row with nodes on one side only, the trunk bends into the branch instead of ending in a T
          const lone = last && !tiles.some(t => Math.sign(t.cx - x) === -side);
          under.push(path(lone ? [[x, y - 2 * R], [x, y], [reach, y]] : [[x, y], [reach, y]], trunk));
          here.forEach((t, k) => {
            const isOuter = k === here.length - 1;
            over.push(path(isOuter ? [[reach, y], [t.cx, y], [t.cx, t.t]] : [[t.cx, y], [t.cx, t.t]], `drop ${t.state}`));
          });
        }
      });
    }
  }
  svg.setAttribute('viewBox', `0 0 ${box.width} ${box.height}`);
  svg.setAttribute('width', box.width);
  svg.setAttribute('height', box.height);
  svg.innerHTML = under.join('') + over.join('');
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

/* ─── Details of the selected server ───────────────────────────────────── */
let selectedId = null;

/** The network side of the selected server only: four groups of three rows, so they line up. */
function renderDetail() {
  const el = document.getElementById('net-detail');
  if (!el) return;
  const n = nodes.find(x => x.id === selectedId) || nodes.find(x => x.is_local);
  if (!n) { el.innerHTML = ''; return; }
  const meta = n.metadata || {};
  const t = n.traffic;
  const link = linkOf(n);
  const ping = pings.get(n.id);
  const remote = nodes.filter(x => !x.is_local);
  // Every row is always there (a dash when unknown), so the groups stay the same length
  const row = (label, value, cls = '') =>
    `<div><dt>${label}</dt><dd class="${value == null || value === '' ? 'none' : cls}">${esc(value == null || value === '' ? '—' : value)}</dd></div>`;
  const group = (title, rows) => `<div class="nd-group"><h3>${title}</h3><dl>${rows.join('')}</dl></div>`;
  const rtt = v => !ping ? null : v != null ? ms(v) : 'no reply';
  const traffic = group('Traffic now', [
    row('In', t ? speed(t.in) : null),
    row('Out', t ? speed(t.out) : null),
    row('Total', t ? speed(t.in + t.out) : null),
  ]);

  let groups;
  if (n.is_local) {
    const on = mesh && mesh.enabled;
    const count = key => remote.filter(x => linkOf(x).key === key).length;
    groups = [
      group('Entry point', [
        row('Panel address', location.host, 'mono'),
        row('Public address', meta.ip, 'mono'),
        row('Visitors on', 'TCP 80 / 443'),
      ]),
      group('Node network', [
        row('Hub address', on ? mesh.subnet.replace(/\.0\/\d+$/, '.1') : 'off', 'mono'),
        row('Subnet', on ? mesh.subnet : null, 'mono'),
        row('Tunnels on', on ? `UDP ${mesh.port}` : null),
      ]),
      group('Tunnels', [
        row('Connected', `${count('up')} of ${remote.length}`),
        row('Down', String(count('down')), count('down') ? 'bad' : ''),
        row('No tunnel', String(count('idle')), count('idle') ? 'warn' : ''),
      ]),
      traffic,
    ];
  } else {
    groups = [
      group('Tunnel', [
        row('Status', link.label, link.key === 'up' ? '' : link.key === 'down' ? 'bad' : 'warn'),
        row('Tunnel address', n.wg_ip, 'mono'),
        row('Last handshake', n.wg_handshake_at ? timeAgo(n.wg_handshake_at) : null),
      ]),
      group('Address', [
        row('Public address', n.public_host || meta.ip, 'mono'),
        row('Hostname', meta.hostname, 'mono'),
        row(n.status === 'online' ? 'Online for' : 'Last seen', n.status === 'online' ? (n.online_since ? sinceOnline(n) : 'just connected') : timeAgo(n.last_seen)),
      ]),
      group('Latency', [
        row('Through tunnel', rtt(ping && ping.wg)),
        row('To the agent', rtt(ping && ping.agent)),
        row('Last heartbeat', n.last_seen ? timeAgo(n.last_seen) : null),
      ]),
      traffic,
    ];
  }

  el.innerHTML = `
    <div class="nd-head">
      <i class="tn-dot ${link.key}"></i>
      <strong>${esc(n.name)}</strong>
      <span>${esc(n.is_local ? 'Primary · entry point and hub' : link.label)}</span>
      <a class="nd-open" href="/node?id=${n.id}">Open server page →</a>
    </div>
    <div class="nd-groups">${groups.join('')}</div>`;
}

/* ─── How it works ─────────────────────────────────────────────────────── */
function renderHow() {
  const el = document.getElementById('net-how');
  if (!el) return;
  const on = mesh && mesh.enabled;
  const subnet = on ? mesh.subnet : '10.77.0.0/16';
  const port = on ? mesh.port : 51820;
  const step = (title, text) => `<li><strong>${title}</strong><p>${text}</p></li>`;
  el.innerHTML = [
    step('Everything comes in at the primary',
      'Visitors reach your apps over HTTPS on ports 80 and 443 of the primary. Nginx there ends the encryption, '
      + 'picks a healthy instance of the app and sends the request on. A node never needs to be reachable from the internet.'),
    step('Every node has its own encrypted tunnel',
      `Each node joins a private WireGuard network (<code>${esc(subnet)}</code>) by connecting <em>out</em> to UDP port `
      + `<code>${esc(String(port))}</code> on the primary, so it needs no open ports and no public IP. It gets its own address `
      + 'in that network; the primary is the hub at the first address.'),
    step('The primary reaches instances through the tunnel',
      'A request for an instance on a node goes from nginx through that node\u2019s tunnel to the instance\u2019s port on the '
      + 'node\u2019s tunnel address. Traffic between nodes, such as an app talking to a database on another node, also runs '
      + 'through the primary.'),
    step('How the numbers are measured',
      'Every node sends a heartbeat every 5 seconds with its load and network counters; the primary turns two readings into '
      + 'the traffic shown here. A node that stays silent for about 10 seconds is marked offline and its instances leave the '
      + 'load balancer until it is back. While this page is open, the primary pings every node through its tunnel every '
      + '5 seconds (the tunnel round trip) and sends a message to its agent and back (the agent round trip).'),
  ].join('');
}

function render() {
  renderKpis();
  renderMap();
  renderDetail();
  renderTable();
  renderHow();
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
  // Selecting a server on the map shows its details under it
  document.getElementById('net-map')?.addEventListener('click', e => {
    const target = e.target.closest('[data-select]');
    if (!target || !target.dataset.select) return;
    selectedId = Number(target.dataset.select);
    renderMap();
    renderDetail();
  });
  refresh();
  setInterval(refresh, REFRESH_MS);
  // Cards move when the window changes width (nodes wrap to a new row): the wires follow
  const map = document.getElementById('net-map');
  if (map && 'ResizeObserver' in window) {
    new ResizeObserver(() => {
      // Crossing the width where the nodes split around the trunk: lay them out again; otherwise just the wires
      if (splitMode !== null && (map.clientWidth >= SPLIT_FROM_PX) !== splitMode) renderMap();
      else drawWires();
    }).observe(map);
  }
  document.fonts?.ready.then(drawWires);         // the web fonts change the size of the cards once they are in
}
