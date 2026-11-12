// The public status page: no sign-in, reads /api/status and refreshes itself.
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const STATE = { up: 'Operational', down: 'Not reachable', maintenance: 'Maintenance', stopped: 'Stopped', unknown: 'No data yet' };
const BANNER = {
  up: 'All systems operational',
  down: 'Some systems are not reachable',
  maintenance: 'Scheduled maintenance',
  stopped: 'No services are running',
  unknown: 'Waiting for the first checks',
  none: 'Nothing to show yet',
};
const ICON = {
  up: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16 9.5"/>',
  down: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
  maintenance: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.4 2.4-2.3-.7-.7-2.3z"/>',
  stopped: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5h5v5h-5z"/>',
  unknown: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/>',
  none: '<circle cx="12" cy="12" r="9"/>',
};

const fmtTime = iso => new Date(iso).toLocaleTimeString([], { timeStyle: 'short' });
const fmtDay = iso => new Date(iso + 'T00:00:00').toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
const fmtDayOf = iso => new Date(iso).toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' });

function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}

function duration(from, until) {
  const s = Math.max(60, Math.round(((until ? new Date(until) : new Date()) - new Date(from)) / 1000));
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return s < 172800 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 86400)} days`;
}

function dayCell(d) {
  const cls = d.uptime === null ? '' : d.uptime >= 99.9 ? 'ok' : d.uptime >= 95 ? 'some' : 'bad';
  const text = d.uptime === null ? 'No data' : `${d.uptime}% up`;
  return `<span class="st-day ${cls}" data-tip="${esc(`<b>${fmtDay(d.day)}</b>\n${text}`)}"></span>`;
}

function appCard(a) {
  const last = a.last_check;
  const up = a.uptime === null ? 'No data yet' : `<b>${a.uptime}%</b> uptime`;
  const detail = [a.average_ms ? `${a.average_ms} ms` : '', last ? `checked ${ago(last.at)}` : ''].filter(Boolean).join(' · ');
  return `<section class="st-app">
    <div class="st-app-top"><h3>${esc(a.name)}</h3><span class="st-pill ${a.status}"><i></i>${esc(STATE[a.status] || a.status)}</span><span class="st-up">${up}</span></div>
    <div class="st-days">${a.days.map(dayCell).join('')}</div>
    <div class="st-axis"><span>90 days ago</span><span>${esc(detail)}</span><span>Today</span></div>
  </section>`;
}

function incidents(apps) {
  const all = apps.flatMap(a => (a.incidents || []).map(i => ({ ...i, name: a.name })))
    .sort((x, y) => new Date(y.from) - new Date(x.from));
  $('st-inc-wrap').hidden = false;
  if (!all.length) { $('st-incidents').innerHTML = '<p class="st-empty">No incidents in the last 7 days.</p>'; return; }
  let html = '', day = '';
  for (const i of all) {
    const d = fmtDayOf(i.from);
    if (d !== day) { html += `<div class="st-date">${esc(d)}</div>`; day = d; }
    html += `<div class="st-inc ${i.until ? '' : 'open'}"><i></i><div><strong>${esc(i.name)}</strong> ${i.until ? 'was not reachable' : 'is not reachable'}
      <small>${esc(fmtTime(i.from))}${i.until ? ` – ${esc(fmtTime(i.until))} · ${esc(duration(i.from, i.until))}` : ` · ongoing for ${esc(duration(i.from, null))}`}</small></div></div>`;
  }
  $('st-incidents').innerHTML = html;
}

async function load() {
  try {
    const res = await fetch('/api/status', { cache: 'no-store' });
    if (!res.ok) throw new Error();
    const data = await res.json();
    $('st-banner').className = `st-banner ${data.overall}`;
    $('st-icon').innerHTML = ICON[data.overall] || ICON.none;
    $('st-title').textContent = BANNER[data.overall] || BANNER.none;
    $('st-apps-h').hidden = !data.apps.length;
    $('st-apps').innerHTML = data.apps.map(appCard).join('');
    if (data.apps.length) incidents(data.apps); else $('st-inc-wrap').hidden = true;
    $('st-live').classList.remove('lost');
    $('st-time').textContent = `Updated ${new Date().toLocaleTimeString([], { timeStyle: 'short' })}`;
  } catch {
    $('st-live').classList.add('lost');
    $('st-time').textContent = 'Connection lost, retrying…';
  }
}

// A tooltip on the day bars
const tip = $('st-tip');
document.addEventListener('mousemove', e => {
  const cell = e.target.closest?.('[data-tip]');
  tip.hidden = !cell;
  if (!cell) return;
  tip.innerHTML = cell.dataset.tip.replace(/\n/g, '<br>');   // the text was escaped when it was made
  tip.style.left = `${Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${e.clientY + 16}px`;
});

load();
setInterval(load, 30000);
