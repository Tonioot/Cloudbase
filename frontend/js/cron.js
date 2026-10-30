// Cron jobs: the schedule editor (new-app wizard and Settings), the summary
// line in the header and the Runs tab.

import { api } from './api.js';
import { timeAgo, toast, confirm } from './utils.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const PRESETS = [
  ['*/15 * * * *', 'Every 15 min'],
  ['0 * * * *', 'Hourly'],
  ['0 3 * * *', 'Daily at 03:00'],
  ['0 9 * * 1-5', 'Weekdays 09:00'],
  ['0 9 * * 1', 'Mondays 09:00'],
  ['0 0 1 * *', 'Monthly'],
];
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

export function scheduleEditorHTML(p) {
  return `
    <div class="cron-editor" id="${p}-editor">
      <div class="field">
        <label class="field-label" for="${p}-schedule">Schedule</label>
        <input class="input input-mono" id="${p}-schedule" placeholder="0 3 * * *" spellcheck="false" autocomplete="off" />
        <div class="cron-presets" role="group" aria-label="Common schedules">
          ${PRESETS.map(([expr, label]) => `<button type="button" class="cron-preset" data-expr="${expr}">${label}</button>`).join('')}
        </div>
        <div class="cron-preview" id="${p}-preview" aria-live="polite"></div>
        <div class="field-hint">Five fields: minute, hour, day of month, month, day of week — e.g. <code>*/15 * * * *</code> or <code>30 2 * * 1-5</code>. <code>@daily</code> and <code>@hourly</code> work too.</div>
      </div>
      <div class="cron-pair">
        <div class="field">
          <label class="field-label" for="${p}-tz">Timezone</label>
          <select class="input" id="${p}-tz"></select>
        </div>
        <div class="field">
          <label class="field-label" for="${p}-timeout">Stop a run after</label>
          <select class="input" id="${p}-timeout">
            ${TIMEOUTS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}
          </select>
        </div>
      </div>
    </div>`;
}

/** Wire the editor; returns { get(), set(values), valid() }. */
export function wireScheduleEditor(root, p, initial = {}) {
  const $ = id => root.querySelector(`#${p}-${id}`);
  const input = $('schedule'), tzSel = $('tz'), timeoutSel = $('timeout'), preview = $('preview');
  let valid = false, timer = null, seq = 0;

  const fillTimezones = tz => {
    tzSel.innerHTML = timezones(tz).map(z => `<option value="${esc(z)}" ${z === tz ? 'selected' : ''}>${esc(z.replace(/_/g, ' '))}</option>`).join('');
  };

  const check = async () => {
    const schedule = input.value.trim();
    root.querySelectorAll('.cron-preset').forEach(b => b.classList.toggle('active', b.dataset.expr === schedule));
    if (!schedule) { valid = false; preview.innerHTML = ''; return; }
    const mine = ++seq;
    try {
      const res = await api.previewCron({ schedule, timezone: tzSel.value });
      if (mine !== seq) return;
      valid = true;
      preview.dataset.tone = 'ok';
      preview.innerHTML = `<strong>${esc(res.description)}</strong><span>Next: ${res.next_runs.slice(0, 3).map(r => esc(fmtTime(r, tzSel.value))).join(' · ')}</span>`;
    } catch (e) {
      if (mine !== seq) return;
      valid = false;
      preview.dataset.tone = 'error';
      preview.textContent = e.message;
    }
  };
  const later = () => { clearTimeout(timer); timer = setTimeout(check, 250); };

  input.addEventListener('input', later);
  tzSel.addEventListener('change', check);
  root.querySelectorAll(`#${p}-editor .cron-preset`).forEach(b => {
    b.addEventListener('click', () => { input.value = b.dataset.expr; check(); });
  });

  const api_ = {
    get: () => ({ schedule: input.value.trim(), timezone: tzSel.value === 'UTC' ? '' : tzSel.value, timeout_seconds: parseInt(timeoutSel.value, 10) }),
    set(v) {
      input.value = v.schedule || '';
      fillTimezones(v.timezone || browserTimezone());
      timeoutSel.value = String(v.timeout_seconds || 3600);
      check();
    },
    valid: () => valid,
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
