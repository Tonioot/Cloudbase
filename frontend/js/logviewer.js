// Structured log viewer for Cloudbase's own logs (System logs page and the
// "Cloudbase" source in an app's Logs tab). Entries come from the backend as
// {ts, level, component, text, cont?}; filters run on the server for the
// initial load and in the browser for live entries.

const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };
const MAX_ENTRIES = 5000;

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function fallbackClass(text) {
  if (/\[(error|crit|alert|emerg)\]|error|fail|fatal|exception/i.test(text)) return 'log-error';
  if (/\[warn\]|warn/i.test(text)) return 'log-warn';
  return 'log-default';
}

function lineClass(e) {
  if (e.level === 'ERROR' || e.level === 'CRITICAL') return 'log-error';
  if (e.level === 'WARNING') return 'log-warn';
  if (e.level) return 'log-default';
  return fallbackClass(e.text);
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

  function append(entries) {
    if (!entries.length) return;
    const stick = atBottom();
    const frag = document.createDocumentFragment();
    for (const e of entries) {
      // Traceback lines that arrived separately belong to the previous entry.
      if (e.cont) {
        const prev = frag.lastElementChild || out.lastElementChild;
        if (prev?.classList.contains('log-line')) {
          prev.querySelector('.log-text').textContent += '\n' + e.text;
          continue;
        }
      }
      const div = document.createElement('div');
      div.className = `log-line ${lineClass(e)}`;
      div.innerHTML = `<span class="log-text">${esc(e.text)}</span>`;
      frag.appendChild(div);
      count++;
    }
    out.querySelector('.log-empty')?.remove();
    out.appendChild(frag);
    while (count > MAX_ENTRIES && out.firstElementChild) {
      out.firstElementChild.remove();
      count--;
    }
    if (stick) out.scrollTop = out.scrollHeight;
  }

  async function reload() {
    const mySeq = ++seq;
    out.innerHTML = `<div class="log-empty">Loading…</div>`;
    count = 0;
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
    },
  };
}

/** Fill a <select> with "All components" + names, keeping the current choice. */
export function fillComponentSelect(select, names, current) {
  const all = [...new Set([...names, current].filter(Boolean))].sort();
  select.innerHTML = `<option value="">All components</option>` +
    all.map(n => `<option value="${esc(n)}"${n === current ? ' selected' : ''}>${esc(n)}</option>`).join('');
}
