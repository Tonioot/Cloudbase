// The public status page: no sign-in, reads /api/status and refreshes itself.
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STATE = { up: 'Operational', down: 'Not reachable', maintenance: 'Maintenance', stopped: 'Stopped', unknown: 'No data yet' };
const BANNER = {
  up: 'All systems are operational',
  down: 'Some systems are not reachable',
  maintenance: 'Some systems are under maintenance',
  none: 'No systems are shown yet',
};

function when(iso) {
  return new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function duration(from, until) {
  const s = Math.max(60, Math.round(((until ? new Date(until) : new Date()) - new Date(from)) / 1000));
  return s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`;
}

function dayCell(d) {
  const cls = d.uptime === null ? '' : d.uptime >= 99.9 ? 'ok' : d.uptime >= 95 ? 'some' : 'bad';
  const title = `${d.day}: ${d.uptime === null ? 'no data' : d.uptime + '% up'}`;
  return `<span class="st-day ${cls}" title="${esc(title)}"></span>`;
}

function appCard(a) {
  const dot = a.status === 'up' || a.status === 'down' || a.status === 'maintenance' ? a.status : '';
  const incidents = (a.incidents || []).slice(0, 3).map(i =>
    `<div>${i.until ? 'Was not reachable' : 'Not reachable since'} ${esc(when(i.from))}${i.until ? ` for ${esc(duration(i.from, i.until))}` : ''}</div>`).join('');
  return `<section class="st-app">
    <div class="st-app-head"><span class="st-dot ${dot}"></span><strong>${esc(a.name)}</strong><span class="st-state">${esc(STATE[a.status] || a.status)}</span></div>
    <div class="st-days">${a.days.map(dayCell).join('')}</div>
    <div class="st-foot"><span>90 days ago</span><span>${a.uptime === null ? 'No data yet' : `${a.uptime}% uptime`}${a.average_ms ? ` · ${a.average_ms} ms` : ''}</span><span>Today</span></div>
    ${incidents ? `<div class="st-inc">${incidents}</div>` : ''}
  </section>`;
}

async function load() {
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    const banner = $('st-banner');
    banner.hidden = false;
    banner.className = `st-banner ${data.overall}`;
    $('st-banner-text').textContent = BANNER[data.overall] || '';
    $('st-apps').innerHTML = data.apps.length ? data.apps.map(appCard).join('') : '<p class="st-empty">Nothing is shown here yet.</p>';
    $('st-updated').textContent = `Updated ${new Date().toLocaleTimeString([], { timeStyle: 'short' })}. This page refreshes itself.`;
  } catch {
    $('st-updated').textContent = 'Could not reach the server. Trying again…';
  }
}

load();
setInterval(load, 30000);
