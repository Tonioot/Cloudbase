// The public status page: no sign-in, reads /api/status and refreshes itself.
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STATE = { up: 'Operational', down: 'Not reachable', maintenance: 'Maintenance', stopped: 'Stopped', unknown: 'No data yet' };
const BANNER = {
  up: ['All systems operational', 'Everything is working as it should.'],
  down: ['Some systems are not reachable', 'This page updates by itself.'],
  maintenance: ['Scheduled maintenance', 'Some systems are being worked on.'],
  none: ['Nothing to show yet', 'No systems have been added to this page.'],
};
const ICON = {
  up: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16 9.5"/>',
  down: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
  maintenance: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.4 2.4-2.3-.7-.7-2.3z"/>',
  none: '<circle cx="12" cy="12" r="9"/>',
};

const fmtDate = iso => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
const fmtDay = iso => new Date(iso + 'T00:00:00').toLocaleDateString([], { dateStyle: 'medium' });

function duration(from, until) {
  const s = Math.max(60, Math.round(((until ? new Date(until) : new Date()) - new Date(from)) / 1000));
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`;
}

function dayCell(d) {
  const cls = d.uptime === null ? '' : d.uptime >= 99.9 ? 'ok' : d.uptime >= 95 ? 'some' : 'bad';
  const text = d.uptime === null ? 'No data' : `${d.uptime}% up`;
  return `<span class="st-day ${cls}" data-tip="${esc(fmtDay(d.day) + '\n' + text)}"></span>`;
}

function spark(values) {
  const ok = values.filter(v => v !== null);
  if (ok.length < 2) return '';
  const max = Math.max(...ok, 1), w = 140, h = 32, step = w / (values.length - 1);
  let path = '', pen = false;
  values.forEach((v, i) => {
    if (v === null) { pen = false; return; }
    path += `${pen ? 'L' : 'M'}${(i * step).toFixed(1)} ${(h - 3 - (v / max) * (h - 6)).toFixed(1)}`;
    pen = true;
  });
  return `<svg class="st-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="Response time, last hour"><path d="${path}" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>`;
}

function appCard(a) {
  const up = a.uptime === null ? '–' : `${a.uptime}%`;
  const ms = a.average_ms ? `${a.average_ms} ms` : '–';
  return `<section class="st-app">
    <div class="st-app-head"><span class="st-dot ${a.status}"></span><strong>${esc(a.name)}</strong><span class="st-state ${a.status}">${esc(STATE[a.status] || a.status)}</span></div>
    <div class="st-stats">
      <div class="st-stat"><span>Uptime, 90 days</span><b>${up}</b></div>
      <div class="st-stat"><span>Response time</span><b>${ms}</b></div>
      ${spark(a.recent_ms || [])}
    </div>
    <div class="st-days">${a.days.map(dayCell).join('')}</div>
    <div class="st-axis"><span>90 days ago</span><span>Today</span></div>
  </section>`;
}

function incidents(apps) {
  const all = apps.flatMap(a => (a.incidents || []).map(i => ({ ...i, name: a.name })))
    .sort((x, y) => new Date(y.from) - new Date(x.from));
  $('st-inc-wrap').hidden = false;
  $('st-incidents').innerHTML = all.length ? all.map(i => `<div class="st-inc ${i.until ? '' : 'open'}">
      <strong>${esc(i.name)}</strong> ${i.until ? 'was not reachable' : 'is not reachable'}
      <small>${esc(fmtDate(i.from))} · ${i.until ? `lasted ${esc(duration(i.from, i.until))}` : `for ${esc(duration(i.from, null))} so far`}</small></div>`).join('')
    : '<p class="st-empty">No incidents in the last 7 days.</p>';
}

async function load() {
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    const [title, sub] = BANNER[data.overall] || BANNER.none;
    $('st-banner').className = `st-banner ${data.overall}`;
    $('st-icon').innerHTML = ICON[data.overall] || ICON.none;
    $('st-title').textContent = title;
    $('st-sub').textContent = sub;
    $('st-apps-h').hidden = !data.apps.length;
    $('st-apps').innerHTML = data.apps.map(appCard).join('');
    if (data.apps.length) incidents(data.apps);
    $('st-time').textContent = `Updated ${new Date().toLocaleTimeString([], { timeStyle: 'short' })}`;
  } catch {
    $('st-time').textContent = 'Connection lost, retrying…';
  }
}

// A tooltip on the day bars
const tip = $('st-tip');
document.addEventListener('mousemove', e => {
  const cell = e.target.closest?.('[data-tip]');
  tip.hidden = !cell;
  if (!cell) return;
  tip.innerHTML = cell.dataset.tip.split('\n').map(esc).join('<br>');
  tip.style.left = `${Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${e.clientY + 16}px`;
});

load();
setInterval(load, 30000);
