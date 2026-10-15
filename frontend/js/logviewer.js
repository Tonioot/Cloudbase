// Structured log viewer for Cloudbase's own logs (System logs page and the
// "Cloudbase" source in an app's Logs tab). Entries come from the backend as
// {ts, level, component, text, cont?}; filters run on the server for the
// initial load and in the browser for live entries.

import { ansiToHtml } from './utils.js';

const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const MAX_ENTRIES = 5000;

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// One stable color per component, so "build" or "autoscaler" is recognisable at a glance.
// Red stays reserved for errors.
const COMPONENT_COLORS = {
  deploys: 'var(--accent)', build: 'var(--purple)', instances: 'var(--green)',
  autoscaler: 'var(--orange)', nginx: 'var(--yellow)', apps: 'var(--accent)',
  audit: 'var(--text-2)', agent: 'var(--green)', tunnel: 'var(--purple)',
};
const PALETTE = ['var(--accent)', 'var(--purple)', 'var(--green)', 'var(--orange)', 'var(--yellow)'];

function componentColor(name) {
  if (COMPONENT_COLORS[name]) return COMPONENT_COLORS[name];
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

function fallbackLevel(text) {
  if (/\[(error|crit|alert|emerg)\]|\berror\b|failed|fatal|exception/i.test(text)) return 'ERROR';
  if (/\[warn\]|\bwarn(ing)?\b/i.test(text)) return 'WARNING';
  return null;
}

// "[autoscaler] app_id=1 Scaled UP…" → "Scaled UP…" when the component already says it;
// other leading [tags] ("[ZD]", "[Docker build]") become a subtle chip.
function messageHtml(msg, component) {
  let m = String(msg ?? '');
  const tag = /^\[([^\]]{1,24})\]\s*/.exec(m);
  let chip = '';
  if (tag) {
    m = m.slice(tag[0].length);
    if (tag[1].toLowerCase() !== String(component).toLowerCase()) chip = `<span class="cblog-tag">${esc(tag[1])}</span>`;
    m = m.replace(/^app_id=\d+\s+/, '');
  }
  return chip + ansiToHtml(m);
}

function fmtTime(ts) {
  // "2026-10-15 21:30:54,243" → "21:30:54"
  return ts ? ts.slice(11, 19) : '';
}

function fmtDay(ts) {
  const d = new Date(ts.slice(0, 10) + 'T00:00:00');
  return isNaN(d) ? ts.slice(0, 10) : d.toLocaleDateString('nl', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
}

/**
 * @param {HTMLElement} out        the .log-terminal element
 * @param {object} opts
 * @param {(f) => Promise<{entries, components}>} opts.load   initial/filtered load
 * @param {(onEntries) => {close()}} opts.stream              live entries
 * @param {(names: string[]) => void} [opts.onComponents]     component list for a filter
 */
export function createLogViewer(out, { load, stream, onComponents }) {
  const filters = { component: '', level: '', q: '' };
  let count = 0;
  let seq = 0;
  let live = null;

  const matches = e =>
    (!filters.component || e.component === filters.component) &&
    (!filters.level || (LEVELS[e.level] || 0) >= LEVELS[filters.level]) &&
    (!filters.q || e.text.toLowerCase().includes(filters.q.toLowerCase()));

  const atBottom = () => out.scrollHeight - out.scrollTop - out.clientHeight < 40;
  let lastDay = null;

  function addContinuation(row, text) {
    let cont = row.querySelector('.cblog-cont');
    if (!cont) {
      cont = document.createElement('div');
      cont.className = 'cblog-cont';
      row.querySelector('.cblog-body').appendChild(cont);
      cont.textContent = text;
    } else {
      cont.textContent += '\n' + text;
    }
  }

  function rowFor(e) {
    const level = e.level || fallbackLevel(e.text);
    const lvl = level === 'CRITICAL' ? 'ERROR' : level;
    const row = document.createElement('div');
    row.className = `cblog-row${lvl === 'ERROR' ? ' cblog-row--error' : lvl === 'WARNING' ? ' cblog-row--warn' : ''}`;
    const [first, ...rest] = String(e.msg ?? e.text).split('\n');
    const badge = lvl === 'ERROR' ? '<span class="cblog-level cblog-level--error">ERROR</span>'
      : lvl === 'WARNING' ? '<span class="cblog-level cblog-level--warn">WARN</span>' : '';
    // Raw lines without a parsed timestamp (nginx, start.sh) show as plain text.
    row.innerHTML = e.ts
      ? `<span class="cblog-time" title="${esc(e.ts)}">${fmtTime(e.ts)}</span>` +
        `<span class="cblog-comp" style="--cc:${componentColor(e.component)}">${esc(e.component)}</span>` +
        `<span class="cblog-body">${badge}<span class="cblog-msg">${messageHtml(first, e.component)}</span></span>`
      : `<span class="cblog-body cblog-body--raw">${badge}<span class="cblog-msg">${ansiToHtml(first)}</span></span>`;
    if (rest.length) addContinuation(row, rest.join('\n'));
    return row;
  }

  function append(entries) {
    if (!entries.length) return;
    const stick = atBottom();
    const frag = document.createDocumentFragment();
    for (const e of entries) {
      // Traceback lines that arrived separately belong to the previous entry.
      if (e.cont) {
        const prev = frag.lastElementChild || out.lastElementChild;
        if (prev?.classList.contains('cblog-row')) {
          addContinuation(prev, e.msg ?? e.text);
          continue;
        }
      }
      const day = e.ts ? e.ts.slice(0, 10) : null;
      if (day && day !== lastDay) {
        const sep = document.createElement('div');
        sep.className = 'cblog-day';
        sep.textContent = fmtDay(e.ts);
        frag.appendChild(sep);
        lastDay = day;
      }
      frag.appendChild(rowFor(e));
      count++;
    }
    out.querySelector('.log-empty')?.remove();
    out.appendChild(frag);
    while (count > MAX_ENTRIES && out.firstElementChild) {
      if (out.firstElementChild.classList.contains('cblog-row')) count--;
      out.firstElementChild.remove();
    }
    if (stick) out.scrollTop = out.scrollHeight;
  }

  async function reload() {
    const mySeq = ++seq;
    out.innerHTML = `<div class="log-empty">Loading…</div>`;
    out.classList.add('cblog');
    count = 0;
    lastDay = null;
    try {
      const data = await load({ ...filters });
      if (mySeq !== seq) return;
      onComponents?.(data.components || []);
      out.innerHTML = '';
      if (!data.entries.length) {
        const filtered = filters.component || filters.level || filters.q;
        out.innerHTML = `<div class="log-empty">${filtered ? 'No entries match these filters.' : 'Nothing logged yet.'} New entries appear here live.</div>`;
      } else {
        append(data.entries);
      }
      out.scrollTop = out.scrollHeight;
    } catch (e) {
      if (mySeq !== seq) return;
      out.innerHTML = `<div class="log-empty">${esc(e.message || 'Could not load this log.')}</div>`;
    }
  }

  return {
    filters,
    async start() {
      await reload();
      live?.close();
      // A continuation line is shown only when the entry it belongs to was.
      let lastShown = false;
      live = stream(entries => append(entries.filter(e => {
        if (e.cont) return lastShown;
        lastShown = matches(e);
        return lastShown;
      })));
    },
    setFilter(key, value) {
      filters[key] = value;
      return reload();
    },
    stop() {
      seq++;
      live?.close();
      live = null;
      out.classList.remove('cblog');  // the app page reuses this terminal for raw logs
    },
  };
}

/** Fill a <select> with "All components" + names, keeping the current choice. */
export function fillComponentSelect(select, names, current) {
  const all = [...new Set([...names, current].filter(Boolean))].sort();
  select.innerHTML = `<option value="">All components</option>` +
    all.map(n => `<option value="${esc(n)}"${n === current ? ' selected' : ''}>${esc(n)}</option>`).join('');
}
