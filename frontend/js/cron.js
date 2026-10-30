// Cron jobs: the schedule editor (new-app wizard and Settings), the summary
// line in the header and the Runs tab.

import { api } from './api.js';
import { timeAgo, toast, confirm } from './utils.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const TIMEOUTS = [
  [60, '1 minute'], [300, '5 minutes'], [900, '15 minutes'], [1800, '30 minutes'], [3600, '1 hour'],
  [10800, '3 hours'], [21600, '6 hours'], [43200, '12 hours'], [86400, '24 hours'],
];

export const browserTimezone = () => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
};

function timezones(current) {
  let list = [];
  try { list = Intl.supportedValuesOf('timeZone'); } catch { /* older browser */ }
  const all = new Set(['UTC', ...list]);
  if (current) all.add(current);
  return [...all];
}

/** "in 5 min", "in 3 h", "tomorrow 03:00"-ish, for a future time. */
export function timeUntil(iso) {
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (s < 45) return 'in a moment';
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  if (s < 86400) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} d`;
}

function fmtTime(iso, tz) {
  try {
    return new Date(iso).toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: tz || undefined });
  } catch {
    return new Date(iso).toLocaleString();
  }
}

/** Header / list summary: "Every day at 03:00 · next run in 9 h". */
export function cronSummary(app) {
  const c = app.cron;
  if (!c) return '';
  if (app.status === 'running') return `${c.description} · running now`;
  if (!c.enabled) return `${c.description} · paused`;
  return c.next_run_at ? `${c.description} · next run ${timeUntil(c.next_run_at)}` : c.description;
}

/* ─── Schedule editor ───────────────────────────────────────────────────── */
// "Simple" builds the cron expression from chips — each row has common values
// and a field to type any other — "Cron" is for anything else.

const WEEKDAYS = [[1, 'Mon'], [2, 'Tue'], [3, 'Wed'], [4, 'Thu'], [5, 'Fri'], [6, 'Sat'], [0, 'Sun']];
const FREQS = [['minutes', 'Minutes'], ['hours', 'Hours'], ['day', 'Daily'], ['week', 'Weekly'], ['month', 'Monthly']];
const pad = n => String(n).padStart(2, '0');

/** "3", "3:5", "0305", "15.30" → "03:05" / "15:30", or null. */
function normTime(text) {
  const t = String(text || '').trim().replace('.', ':');
  let m = t.match(/^(\d{1,2})(?::(\d{1,2}))?$/) || t.match(/^(\d{2})(\d{2})$/);
  if (!m) return null;
  const h = +m[1], mi = m[2] === undefined ? 0 : +m[2];
  return h < 24 && mi < 60 ? `${pad(h)}:${pad(mi)}` : null;
}

/** Simple choices → cron expression. */
export function simpleToCron(s) {
  const [h, m] = (s.time || '03:00').split(':').map(n => parseInt(n, 10) || 0);
  switch (s.freq) {
    case 'minutes': return s.every === 1 ? '* * * * *' : `*/${s.every} * * * *`;
    case 'hours':   return `${s.minute} ${s.every === 1 ? '*' : `*/${s.every}`} * * *`;
    case 'week': {
      const days = [...new Set(s.days)].sort((a, b) => a - b);
      return `${m} ${h} * * ${days.length ? days.join(',') : '1'}`;
    }
    case 'month':   return `${m} ${h} ${s.day} * *`;
    default:        return `${m} ${h} * * *`;
  }
}

/** Cron expression → simple choices, or null when it doesn't fit one. */
export function cronToSimple(expr) {
  const f = (expr || '').trim().split(/\s+/);
  const macro = { '@hourly': '0 * * * *', '@daily': '0 0 * * *', '@midnight': '0 0 * * *', '@weekly': '0 0 * * 0', '@monthly': '0 0 1 * *' }[f[0]];
  if (macro) return cronToSimple(macro);
  if (f.length !== 5) return null;
  const [mi, ho, dom, mon, dow] = f;
  const num = (v, lo, hi) => /^\d+$/.test(v) && +v >= lo && +v <= hi ? +v : null;
  const step = (v, hi) => v === '*' ? 1 : (/^\*\/\d+$/.test(v) && +v.slice(2) >= 1 && +v.slice(2) <= hi ? +v.slice(2) : null);
  if (mon !== '*') return null;
  if (ho === '*' && dom === '*' && dow === '*') {
    const every = step(mi, 59);
    if (every) return { freq: 'minutes', every };
  }
  const minute = num(mi, 0, 59);
  if (minute === null) return null;
  if (dom === '*' && dow === '*' && ho.includes('*')) {
    const every = step(ho, 23);
    return every ? { freq: 'hours', every, minute } : null;
  }
  const hour = num(ho, 0, 23);
  if (hour === null) return null;
  const time = `${pad(hour)}:${pad(minute)}`;
  if (dom === '*' && dow === '*') return { freq: 'day', time };
  if (dom === '*' && /^[0-7](,[0-7])*$|^[0-7]-[0-7]$/.test(dow)) {
    let days;
    if (dow.includes('-')) {
      const [a, b] = dow.split('-').map(Number);
      if (a > b) return null;
      days = Array.from({ length: b - a + 1 }, (_, i) => a + i);
    } else {
      days = dow.split(',').map(Number);
    }
    return { freq: 'week', days: [...new Set(days.map(d => d % 7))], time };
  }
  const day = num(dom, 1, 31);
  if (day !== null && dow === '*') return { freq: 'month', day, time };
  return null;
}

// One setting row: title and explanation on the left, chips (with an "other"
// field you type in) on the right. kind: number (with min/max) | time
function chipRow(p, key, title, desc, values, { fmt = v => v, other = true, kind = 'number', min, max, unit = '', forFreq }) {
  return `
    <div class="cron-row" data-row="${key}" ${forFreq ? `data-for="${forFreq}"` : ''}>
      <div class="cron-row-label"><span class="cron-row-title">${title}</span><span class="cron-row-desc">${desc}</span></div>
      <div class="cron-chips" role="group" aria-label="${title}" data-key="${key}">
        ${values.map(v => `<button type="button" class="cron-chip" data-value="${v}" aria-pressed="false">${fmt(v)}</button>`).join('')}
        ${other ? `<label class="cron-chip cron-chip-input">
          <input type="text" inputmode="${kind === 'time' ? 'text' : 'numeric'}" data-kind="${kind}" ${min !== undefined ? `data-min="${min}" data-max="${max}"` : ''}
            placeholder="${kind === 'time' ? '--:--' : 'Other'}" maxlength="${kind === 'time' ? 5 : 2}" aria-label="${title}: other value" autocomplete="off" />
          ${unit ? `<span>${unit}</span>` : ''}</label>` : ''}
      </div>
    </div>`;
}

export function scheduleEditorHTML(p) {
  return `
    <div class="cron-editor" id="${p}-editor">
      <div class="cron-top">
        <div class="cron-top-head">
          <div class="cron-row-label"><span class="cron-row-title">Schedule</span><span class="cron-row-desc">How often the job runs</span></div>
          <div class="segmented cron-mode" role="group" aria-label="How to enter the schedule">
            <button type="button" data-mode="simple" aria-pressed="true">Simple</button>
            <button type="button" data-mode="cron" aria-pressed="false">Cron</button>
          </div>
        </div>
        <div class="cron-tabs" id="${p}-tabs" role="tablist" aria-label="How often">
          ${FREQS.map(([v, l]) => `<button type="button" role="tab" data-freq="${v}" aria-selected="false">${l}</button>`).join('')}
        </div>
        <div class="cron-advanced" id="${p}-advanced" hidden>
          <input class="input input-mono" id="${p}-schedule" placeholder="0 3 * * *" spellcheck="false" autocomplete="off" aria-label="Cron expression" />
          <div class="field-hint">Five fields: minute, hour, day of month, month, day of week — e.g. <code>*/15 * * * *</code> or <code>30 2 * * 1-5</code>. <code>@daily</code> and <code>@hourly</code> work too.</div>
        </div>
      </div>

      <div class="cron-simple" id="${p}-simple">
        ${chipRow(p, 'every-min', 'Interval', 'Time between runs', [5, 10, 15, 30], { min: 1, max: 59, unit: 'min', forFreq: 'minutes', fmt: v => `${v} min` })}
        ${chipRow(p, 'every-hour', 'Interval', 'Time between runs', [1, 2, 6, 12], { min: 1, max: 23, unit: 'h', forFreq: 'hours', fmt: v => `${v} h` })}
        ${chipRow(p, 'minute', 'Minute', 'Past every hour', [0, 15, 30, 45], { min: 0, max: 59, forFreq: 'hours', fmt: v => `:${pad(v)}` })}
        ${chipRow(p, 'days', 'Days', 'Days the job runs', WEEKDAYS.map(([d]) => d), { other: false, forFreq: 'week', fmt: d => WEEKDAYS.find(w => w[0] === d)[1] })}
        ${chipRow(p, 'day', 'Day', 'Day of the month', [1, 15, 28], { min: 1, max: 31, forFreq: 'month', fmt: v => v })}
        ${chipRow(p, 'time', 'Time', 'Time of day', ['00:00', '03:00', '09:00', '12:00'], { kind: 'time', forFreq: 'day week month' })}
      </div>

      <div class="cron-summary" id="${p}-preview" aria-live="polite"></div>
      <div class="cron-note" id="${p}-note" hidden></div>

      <div class="cron-row">
        <div class="cron-row-label"><label class="cron-row-title" for="${p}-tz">Timezone</label><span class="cron-row-desc">The times above are in this zone</span></div>
        <div class="cron-tz">
          <input class="input" id="${p}-tz" list="${p}-tzlist" spellcheck="false" autocomplete="off" />
          <datalist id="${p}-tzlist"></datalist>
        </div>
      </div>
      <div class="cron-row">
        <div class="cron-row-label"><span class="cron-row-title">Time limit</span><span class="cron-row-desc">A run that takes longer is stopped</span></div>
        <div class="cron-chips" role="group" aria-label="Time limit per run" data-key="timeout">
          ${TIMEOUTS.map(([v]) => `<button type="button" class="cron-chip" data-value="${v}" aria-pressed="false">${v < 3600 ? `${v / 60}m` : `${v / 3600}h`}</button>`).join('')}
        </div>
      </div>
    </div>`;
}

/** Wire the editor; returns { get(), set(values), valid(), check() }. */
export function wireScheduleEditor(root, p, initial = {}) {
  const $ = id => root.querySelector(`#${p}-${id}`);
  const editor = $('editor');
  const input = $('schedule'), tzInput = $('tz'), preview = $('preview'), note = $('note');
  const zones = timezones(browserTimezone());
  $('tzlist').innerHTML = zones.map(z => `<option value="${esc(z)}"></option>`).join('');
  let mode = 'simple', valid = false, timer = null, seq = 0;
  const state = { freq: 'day', everyMin: 15, everyHour: 1, minute: 0, day: 1, time: '03:00', days: [1, 2, 3, 4, 5], timeout: 3600 };
  const simple = () => ({ ...state, every: state.freq === 'hours' ? state.everyHour : state.everyMin });
  const apply = s => {
    Object.assign(state, s);
    if (s.freq === 'minutes') state.everyMin = s.every;
    if (s.freq === 'hours') state.everyHour = s.every;
  };
  const group = key => editor.querySelector(`.cron-chips[data-key="${key}"]`);

  // Reflect one value in its chip row: press the matching chip, or show it in the field
  const showValue = (key, value) => {
    const g = group(key);
    const field = g.querySelector('input');
    let matched = false;
    g.querySelectorAll('.cron-chip[data-value]').forEach(b => {
      const on = key === 'days' ? value.includes(+b.dataset.value) : b.dataset.value === String(value);
      matched = matched || on;
      b.setAttribute('aria-pressed', String(on));
    });
    if (field && document.activeElement !== field) field.value = matched ? '' : String(value);
    field?.closest('.cron-chip').classList.toggle('active', !!field && !matched && field.value !== '');
  };

  const render = () => {
    editor.querySelectorAll('[data-freq]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.freq === state.freq)));
    editor.querySelectorAll('.cron-simple .cron-row').forEach(r => { r.hidden = !r.dataset.for.split(' ').includes(state.freq); });
    showValue('every-min', state.everyMin);
    showValue('every-hour', state.everyHour);
    showValue('minute', state.minute);
    showValue('days', state.days);
    showValue('day', state.day);
    showValue('time', state.time);
    showValue('timeout', state.timeout);
  };

  const unevenNote = () => {
    const n = state.freq === 'hours' ? state.everyHour : state.everyMin;
    if (mode === 'simple' && state.freq === 'minutes' && 60 % n) {
      return `Cron counts within each hour: this runs at ${[0, n, 2 * n].map(v => `:${pad(v)}`).join(', ')} … :${pad(Math.floor(59 / n) * n)} and then at :00 again, so the gap around the hour is shorter.`;
    }
    if (mode === 'simple' && state.freq === 'hours' && 24 % n) {
      return `Cron counts within each day: this runs at ${[0, n, 2 * n].map(v => `${pad(v)}:${pad(state.minute)}`).join(', ')} … and starts again at 00:${pad(state.minute)}.`;
    }
    return '';
  };

  const fromSimple = () => {
    render();
    input.value = simpleToCron(simple());
    check();
  };

  const setMode = m => {
    mode = m;
    editor.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mode === m)));
    $('simple').hidden = m !== 'simple';
    $('tabs').hidden = m !== 'simple';
    $('advanced').hidden = m !== 'cron';
    if (m === 'simple') fromSimple();
    else check();
  };

  async function check() {
    const schedule = input.value.trim();
    const n = unevenNote();
    note.hidden = !n;
    note.textContent = n;
    if (!schedule) { valid = false; preview.innerHTML = ''; return; }
    const mine = ++seq;
    try {
      const res = await api.previewCron({ schedule, timezone: tzInput.value.trim() || 'UTC' });
      if (mine !== seq) return;
      valid = true;
      preview.dataset.tone = 'ok';
      preview.innerHTML = `
        <span class="cron-summary-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg></span>
        <span class="cron-summary-text">
          <span class="cron-summary-title">${esc(res.description)}</span>
        </span>
        ${res.description.startsWith('Custom') ? '' : `<code class="cron-summary-expr">${esc(res.schedule)}</code>`}`;
    } catch (e) {
      if (mine !== seq) return;
      valid = false;
      preview.dataset.tone = 'error';
      preview.textContent = e.message;
    }
  }
  const later = () => { clearTimeout(timer); timer = setTimeout(check, 250); };

  // Mode switch
  editor.querySelectorAll('[data-mode]').forEach(b => b.addEventListener('click', () => {
    if (b.dataset.mode === mode) return;
    if (b.dataset.mode === 'simple') {
      const s = cronToSimple(input.value);
      if (!s && input.value.trim() && !window.confirm('This schedule can’t be shown in Simple mode. Replace it with a simple schedule?')) return;
      if (s) apply(s);
    }
    setMode(b.dataset.mode);
  }));

  // Frequency tabs
  editor.querySelectorAll('[data-freq]').forEach(b => b.addEventListener('click', () => {
    state.freq = b.dataset.freq;
    fromSimple();
  }));

  const setKey = (key, value) => {
    if (key === 'every-min') state.everyMin = value;
    else if (key === 'every-hour') state.everyHour = value;
    else if (key === 'days') {
      const days = new Set(state.days);
      if (days.has(value) && days.size > 1) days.delete(value); else days.add(value);
      state.days = [...days];
    } else state[key] = value;
    if (key === 'timeout') { render(); return; }
    fromSimple();
  };

  // Chips
  editor.querySelectorAll('.cron-chips').forEach(g => {
    const key = g.dataset.key;
    g.querySelectorAll('.cron-chip[data-value]').forEach(b => b.addEventListener('click', () => {
      const field = g.querySelector('input');
      if (field) field.value = '';
      setKey(key, key === 'time' ? b.dataset.value : +b.dataset.value);
    }));
    const field = g.querySelector('input');
    if (!field) return;
    const read = () => {
      if (field.dataset.kind === 'time') return normTime(field.value);
      const v = parseInt(field.value, 10);
      return /^\d+$/.test(field.value.trim()) && v >= +field.dataset.min && v <= +field.dataset.max ? v : null;
    };
    field.addEventListener('input', () => {
      const v = read();
      field.closest('.cron-chip').classList.toggle('invalid', field.value !== '' && v === null);
      if (v !== null) setKey(key, v);
    });
    field.addEventListener('blur', () => {
      const v = read();
      field.closest('.cron-chip').classList.remove('invalid');
      if (v === null) field.value = '';
      render();
    });
    field.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); field.blur(); } });
  });

  // Cron mode + timezone
  input.addEventListener('input', later);
  tzInput.addEventListener('change', () => {
    const tz = tzInput.value.trim();
    const match = zones.find(z => z.toLowerCase() === tz.toLowerCase());
    tzInput.classList.toggle('invalid', !!tz && !match);
    if (match) tzInput.value = match;
    check();
  });

  const api_ = {
    get: () => ({ schedule: input.value.trim(), timezone: (tzInput.value.trim() === 'UTC' ? '' : tzInput.value.trim()), timeout_seconds: state.timeout }),
    set(v) {
      input.value = v.schedule || '';
      tzInput.value = v.timezone || browserTimezone();
      if (!zones.includes(tzInput.value)) {
        zones.push(tzInput.value);
        $('tzlist').insertAdjacentHTML('beforeend', `<option value="${esc(tzInput.value)}"></option>`);
      }
      state.timeout = v.timeout_seconds || 3600;
      const s = cronToSimple(input.value);
      if (s) { apply(s); setMode('simple'); }
      else { render(); setMode('cron'); }
    },
    valid: () => valid && !tzInput.classList.contains('invalid'),
    check,
  };
  api_.set({ schedule: initial.schedule ?? '0 3 * * *', timezone: initial.timezone, timeout_seconds: initial.timeout_seconds });
  return api_;
}

/* ─── Runs tab ──────────────────────────────────────────────────────────── */

const RUN_LABEL = { running: 'Running', success: 'Succeeded', failed: 'Failed', timeout: 'Timed out' };

function duration(s) {
  if (s == null) return '—';
  if (s < 1) return '<1s';
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
  return `${(s / 3600).toFixed(1)} h`;
}

function runRow(r) {
  const how = r.trigger === 'manual' ? `Manual${r.actor ? ` · ${esc(r.actor)}` : ''}` : 'Scheduled';
  return `
    <tr class="clickable cron-run" data-id="${r.id}" aria-expanded="false">
      <td><span class="run-status" data-status="${r.status}">${RUN_LABEL[r.status] || esc(r.status)}</span></td>
      <td class="cell-mono cell-muted">#${r.id}</td>
      <td title="${esc(new Date(r.started_at).toLocaleString())}">${timeAgo(r.started_at)}</td>
      <td class="cell-mono">${r.status === 'running' ? '…' : duration(r.duration_seconds)}</td>
      <td class="cell-muted">${how}</td>
      <td class="cell-mono cell-muted">${r.exit_code ?? '—'}</td>
    </tr>`;
}

export function initRunsTab(appId, { getApp, onChange }) {
  const panel = document.getElementById('panel-runs');
  let timer = null;
  let open = new Set();

  const render = async () => {
    let data;
    try { data = await api.listCronRuns(appId); }
    catch (e) { panel.querySelector('#runs-table').innerHTML = `<div class="apps-empty">${esc(e.message)}</div>`; return; }
    const c = data.cron;
    const app = getApp();
    const last = data.runs.find(r => r.status !== 'running');
    const ok = data.runs.filter(r => r.status !== 'running').slice(0, 20);
    const rate = ok.length ? Math.round(100 * ok.filter(r => r.status === 'success').length / ok.length) : null;
    panel.querySelector('#runs-summary').innerHTML = `
      <div class="runs-card"><span class="runs-k">Schedule</span><span class="runs-v">${esc(c.description)}</span>
        <span class="runs-sub"><code>${esc(c.schedule)}</code> · ${esc((c.timezone || 'UTC').replace(/_/g, ' '))}</span></div>
      <div class="runs-card"><span class="runs-k">Next run</span>
        <span class="runs-v">${data.status === 'running' ? 'Running now' : !c.enabled ? 'Paused' : c.next_run_at ? timeUntil(c.next_run_at) : '—'}</span>
        <span class="runs-sub">${c.enabled && c.next_run_at ? esc(fmtTime(c.next_run_at)) : !c.enabled ? 'Resume to run on schedule' : ''}</span></div>
      <div class="runs-card"><span class="runs-k">Last run</span>
        <span class="runs-v">${last ? `<span class="run-status" data-status="${last.status}">${RUN_LABEL[last.status]}</span>` : 'Never'}</span>
        <span class="runs-sub">${last ? `${timeAgo(last.started_at)} · ${duration(last.duration_seconds)}` : 'Use Run now to try it'}</span></div>
      <div class="runs-card"><span class="runs-k">Success rate</span>
        <span class="runs-v">${rate == null ? '—' : `${rate}%`}</span>
        <span class="runs-sub">${ok.length ? `Last ${ok.length} run${ok.length === 1 ? '' : 's'}` : ''}</span></div>`;

    const table = panel.querySelector('#runs-table');
    if (!data.runs.length) {
      table.innerHTML = `<div class="empty-state"><div class="empty-title">No runs yet</div>
        <p class="empty-sub">${c.enabled && c.next_run_at ? `The first scheduled run is ${esc(fmtTime(c.next_run_at))}.` : 'This job is paused.'} Use <strong>Run now</strong> to try it.</p></div>`;
      return;
    }
    table.innerHTML = `
      <table class="table runs-table">
        <thead><tr><th>Status</th><th>Run</th><th>Started</th><th>Duration</th><th>Trigger</th><th>Exit</th></tr></thead>
        <tbody>${data.runs.map(r => runRow(r) + (open.has(r.id) ? `<tr class="run-detail" data-for="${r.id}"><td colspan="6"><div class="run-output">Loading…</div></td></tr>` : '')).join('')}</tbody>
      </table>`;
    for (const id of open) loadOutput(id);
    if (app && app.status !== data.status) onChange?.();
  };

  const loadOutput = async id => {
    const cell = panel.querySelector(`tr.run-detail[data-for="${id}"] .run-output`);
    const row = panel.querySelector(`tr.cron-run[data-id="${id}"]`);
    if (row) row.setAttribute('aria-expanded', 'true');
    if (!cell) return;
    try {
      const r = await api.getCronRun(appId, id);
      cell.innerHTML = (r.error ? `<div class="run-error">${esc(r.error)}</div>` : '')
        + (r.output ? `<pre>${esc(r.output)}</pre>` : `<div class="cell-muted">${r.status === 'running' ? 'Output shows up in the Logs tab while the job runs.' : 'No output.'}</div>`);
    } catch (e) { cell.textContent = e.message; }
  };

  panel.querySelector('#runs-table').onclick = e => {
    const row = e.target.closest('tr.cron-run');
    if (!row) return;
    const id = parseInt(row.dataset.id, 10);
    const detail = panel.querySelector(`tr.run-detail[data-for="${id}"]`);
    if (detail) { detail.remove(); open.delete(id); row.setAttribute('aria-expanded', 'false'); return; }
    open.add(id);
    row.insertAdjacentHTML('afterend', `<tr class="run-detail" data-for="${id}"><td colspan="6"><div class="run-output">Loading…</div></td></tr>`);
    loadOutput(id);
  };

  render();
  timer = setInterval(() => { if (!document.hidden) render(); }, 5000);
  return () => { clearInterval(timer); timer = null; };
}

/* ─── Header actions ────────────────────────────────────────────────────── */

export async function runNow(appId, btn) {
  btn.disabled = true;
  try {
    const r = await api.runCronNow(appId);
    toast(`Run #${r.id} started`);
  } catch (e) {
    toast(e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

export async function toggleSchedule(app, btn) {
  const pausing = app.cron?.enabled;
  if (pausing && !await confirm('Pause schedule', `<strong>${esc(app.name)}</strong> won’t run on its schedule until you resume it. A run that is going keeps running.`)) return null;
  btn.disabled = true;
  try {
    const res = pausing ? await api.pauseCron(app.id) : await api.resumeCron(app.id);
    toast(pausing ? 'Schedule paused' : 'Schedule resumed — runs at the next slot');
    return res;
  } catch (e) {
    toast(e.message, 'error');
    return null;
  } finally {
    btn.disabled = false;
  }
}
