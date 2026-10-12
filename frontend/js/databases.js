import { api, PermissionError } from './api.js';
import { toast } from './utils.js';
import { esc, statusColor } from './shell.js';

// Suggested memory per engine: enough to run comfortably, and the limit drives tuning.
const DEFAULT_MEMORY_MB = { postgres: 512, mysql: 512, mariadb: 512, mongodb: 1024, redis: 256, valkey: 256 };
const ENGINE_KIND = {
  postgres: 'Relational', mysql: 'Relational', mariadb: 'Relational',
  mongodb: 'Document', redis: 'Key-value · cache', valkey: 'Key-value · cache',
};

let engines = [];
let pollTimer = null;

export async function initDatabasesPage() {
  document.getElementById('btn-new-database')?.addEventListener('click', openCreateDialog);
  if (new URLSearchParams(location.search).get('action') === 'new') openCreateDialog();
  await loadDatabases();
}

function statusLine(d) {
  if (d.status === 'error' || d.status === 'unhealthy') return { text: d.last_error || 'Needs attention', cls: 'error' };
  if (d.status === 'node_offline') return { text: `Node ${d.node_name || ''} is offline`, cls: 'error' };
  if (d.status === 'creating') return { text: 'Pulling image and initialising…', cls: '' };
  return { text: d.status.replace('_', ' '), cls: '' };
}

function rowHTML(d) {
  const sub = statusLine(d);
  const limits = [d.cpu_limit ? `${d.cpu_limit} CPU` : null, d.memory_limit_mb ? `${d.memory_limit_mb} MB` : null].filter(Boolean).join(' · ');
  return `
    <tr class="clickable" data-db-id="${d.id}">
      <td>
        <div class="app-cell">
          <span class="dot" style="background:${statusColor(d.status)}"></span>
          <span class="app-cell-text">
            <a class="app-cell-name" href="/database?id=${d.id}">${esc(d.name)}</a>
            <span class="app-cell-sub ${sub.cls}">${esc(sub.text)}</span>
          </span>
        </div>
      </td>
      <td class="cell-muted">${esc(d.engine_label)} ${esc(d.version)}</td>
      <td class="cell-mono">${esc(d.node_name || '—')}</td>
      <td class="cell-muted">${esc(limits || 'No limits')}</td>
      <td class="num">${d.link_count || '—'}</td>
    </tr>`;
}

async function loadDatabases() {
  const wrap = document.getElementById('databases-wrap');
  let databases;
  try {
    databases = await api.listDatabases();
  } catch (e) {
    const msg = e instanceof PermissionError ? 'You do not have access to databases (databases.view required).' : e.message;
    wrap.innerHTML = `<div class="apps-empty" style="color:var(--red)">${esc(msg)}</div>`;
    return;
  }

  if (!databases.length) {
    wrap.innerHTML = `
      <div class="apps-empty">
        <strong>No databases yet</strong>
        Create PostgreSQL, MySQL, MariaDB, Redis, Valkey or MongoDB on any node and link it to your apps.
        <div style="margin-top:18px"><button class="btn btn-primary" id="empty-new-database" data-perm="databases.create">New database</button></div>
      </div>`;
    document.getElementById('empty-new-database')?.addEventListener('click', openCreateDialog);
  } else {
    wrap.innerHTML = `
      <div class="apps-table-wrap">
        <table class="table apps-table">
          <thead><tr><th>Name</th><th>Engine</th><th>Node</th><th>Limits</th><th class="num">Linked apps</th></tr></thead>
          <tbody>${databases.map(rowHTML).join('')}</tbody>
        </table>
      </div>`;
    wrap.querySelectorAll('tr[data-db-id]').forEach(tr => {
      tr.addEventListener('click', e => {
        if (e.target.closest('a')) return;
        location.href = `/database?id=${tr.dataset.dbId}`;
      });
    });
  }
  if (window._applyPermVisibility) window._applyPermVisibility(wrap);

  // Poll faster while something is in progress.
  clearTimeout(pollTimer);
  const busy = databases.some(d => ['creating', 'starting', 'stopping'].includes(d.status) || d.busy);
  pollTimer = setTimeout(loadDatabases, busy ? 3000 : 15000);
}

async function openCreateDialog() {
  let nodes;
  try {
    [engines, nodes] = await Promise.all([
      engines.length ? engines : api.listDatabaseEngines(),
      api.listNodes(),
    ]);
  } catch (e) {
    toast(e.message, 'error');
    return;
  }
  const usableNodes = nodes
    .filter(n => n.is_local || (n.enabled && n.status === 'online'))
    .sort((a, b) => (b.is_local ? 1 : 0) - (a.is_local ? 1 : 0));

  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="db-dlg-title">
      <div class="dialog-title" id="db-dlg-title">New database</div>
      <div class="dialog-body">
        <fieldset class="form-block">
          <legend>Engine</legend>
          <div class="choice-list" role="radiogroup">
            ${engines.map((e, i) => `
              <label class="choice">
                <input type="radio" name="db-engine" value="${esc(e.key)}" ${i === 0 ? 'checked' : ''} />
                <span class="choice-dot" aria-hidden="true"></span>
                <span class="choice-text"><span>${esc(e.label)}</span><small>${esc(ENGINE_KIND[e.key] || '')} · port ${e.port}</small></span>
              </label>`).join('')}
          </div>
        </fieldset>

        <div class="form-pair">
          <div class="field" style="margin:0">
            <label class="field-label" for="db-name">Name</label>
            <input id="db-name" class="input input-mono" placeholder="e.g. shop-db" maxlength="32" autocomplete="off" spellcheck="false" />
          </div>
          <div class="field" style="margin:0">
            <label class="field-label" for="db-version">Version</label>
            <select id="db-version" class="input"></select>
          </div>
        </div>

        <fieldset class="form-block">
          <legend>Node <small>the database stays on this node</small></legend>
          <div class="choice-list" role="radiogroup">
            ${usableNodes.map((n, i) => `
              <label class="choice">
                <input type="radio" name="db-node" value="${n.id}" ${i === 0 ? 'checked' : ''} />
                <span class="choice-dot" aria-hidden="true"></span>
                <span class="choice-text"><span>${esc(n.is_local ? 'Primary' : n.name)}</span><small>${esc([n.metadata?.arch, n.public_host].filter(Boolean).join(' · ') || 'online')}</small></span>
              </label>`).join('')}
          </div>
        </fieldset>

        <fieldset class="form-block">
          <legend>Resources <small>memory also sets the cache sizes</small></legend>
          <div class="form-pair">
            <label class="unit-input"><span class="sr-only">CPU limit</span>
              <input id="db-cpu" class="input" type="number" min="0.1" step="0.1" placeholder="no limit" />
              <span class="unit">CPU</span>
            </label>
            <label class="unit-input"><span class="sr-only">Memory limit</span>
              <input id="db-mem" class="input" type="number" min="32" step="64" />
              <span class="unit">MB</span>
            </label>
          </div>
        </fieldset>

        <div class="field-hint" id="db-dlg-error" style="color:var(--red);display:none;margin:0"></div>
      </div>
      <div class="dialog-actions">
        <button class="btn" id="db-dlg-cancel">Cancel</button>
        <button class="btn btn-primary" id="db-dlg-ok">Create database</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);

  const $ = sel => backdrop.querySelector(sel);
  const selectedEngine = () => engines.find(e => e.key === backdrop.querySelector('input[name="db-engine"]:checked')?.value);
  const syncEngine = () => {
    const e = selectedEngine();
    if (!e) return;
    $('#db-version').innerHTML = e.versions.map(v => `<option value="${esc(v)}">${esc(e.label)} ${esc(v)}</option>`).join('');
    $('#db-mem').value = DEFAULT_MEMORY_MB[e.key] || 512;
  };
  backdrop.querySelectorAll('input[name="db-engine"]').forEach(r => r.addEventListener('change', syncEngine));
  syncEngine();
  $('#db-name').focus();

  const close = () => backdrop.remove();
  $('#db-dlg-cancel').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });

  $('#db-dlg-ok').onclick = async () => {
    const btn = $('#db-dlg-ok');
    const err = $('#db-dlg-error');
    const cpu = parseFloat($('#db-cpu').value);
    const mem = parseInt($('#db-mem').value, 10);
    const nodeVal = backdrop.querySelector('input[name="db-node"]:checked')?.value;
    err.style.display = 'none';
    btn.disabled = true;
    btn.textContent = 'Creating…';
    try {
      const created = await api.createDatabase({
        name: $('#db-name').value.trim().toLowerCase(),
        engine: selectedEngine()?.key,
        version: $('#db-version').value,
        node_id: nodeVal ? parseInt(nodeVal, 10) : null,
        cpu_limit: Number.isFinite(cpu) ? cpu : null,
        memory_limit_mb: Number.isInteger(mem) ? mem : null,
      });
      close();
      location.href = `/database?id=${created.id}`;
    } catch (e) {
      err.textContent = e.message;
      err.style.display = '';
      btn.disabled = false;
      btn.textContent = 'Create database';
    }
  };
}
