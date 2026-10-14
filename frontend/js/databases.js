import { api } from './api.js';
import { toast } from './utils.js';
import { esc, statusColor } from './shell.js';
import { engineIcon } from './engine-icons.js';

// Suggested memory per engine: enough to run comfortably, and the limit drives tuning.
const DEFAULT_MEMORY_MB = { postgres: 512, mysql: 512, mariadb: 512, mongodb: 1024, redis: 256, valkey: 256 };
const ENGINE_KIND = {
  postgres: 'Relational', mysql: 'Relational', mariadb: 'Relational',
  mongodb: 'Document', redis: 'Key-value', valkey: 'Key-value',
};
// What a linked app receives, shown live in the dialog (password masked).
const URL_PREVIEW = {
  postgres: (u, db) => `postgresql://${u}:•••@HOST:5432/${db}`,
  mysql:    (u, db) => `mysql://${u}:•••@HOST:3306/${db}`,
  mariadb:  (u, db) => `mysql://${u}:•••@HOST:3306/${db}`,
  redis:    ()      => `redis://default:•••@HOST:6379/0`,
  valkey:   ()      => `redis://default:•••@HOST:6379/0`,
  mongodb:  (u, db) => `mongodb://${u}:•••@HOST:27017/${db}?authSource=admin`,
};

let engines = [];

const fmtGb = mb => mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;

/**
 * Advice when the databases on a node would together be allowed too much of its
 * memory. `excludeId` leaves out the database being edited. Returns '' when fine.
 */
export function memoryWarning(node, databases, newMb, excludeId = null) {
  const total = node?.metadata?.ram_total_mb;
  if (!total) return '';
  const others = databases.filter(d => d.node_id === node.id && d.id !== excludeId && d.desired_state !== 'stopped');
  const unlimited = others.filter(d => !d.memory_limit_mb).length + (newMb ? 0 : 1);
  const sum = others.reduce((n, d) => n + (d.memory_limit_mb || 0), 0) + (newMb || 0);
  const name = node.is_local ? 'the primary' : node.name;
  if (sum > total * 0.6) {
    return `Databases on ${name} would together be allowed ${fmtGb(sum)} of its ${fmtGb(total)} memory. Apps need memory too: lower the limits or stop databases you don't use.`;
  }
  if (unlimited && others.length) {
    return `${unlimited} database${unlimited === 1 ? '' : 's'} on ${name} ${unlimited === 1 ? 'has' : 'have'} no memory limit and can use all of its ${fmtGb(total)}.`;
  }
  return '';
}

function statusLine(d) {
  if (d.status === 'error' || d.status === 'unhealthy') return { text: d.last_error || 'Needs attention', cls: 'error' };
  if (d.status === 'node_offline') return { text: `Node ${d.node_name || ''} is offline`, cls: 'error' };
  if (d.progress) return { text: d.progress, cls: '' };
  if (d.status === 'creating') return { text: 'Preparing…', cls: '' };
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
      <td class="cell-muted"><span class="engine-cell">${engineIcon(d.engine, 22)}${esc(d.engine_label)} ${esc(d.version)}</span></td>
      <td class="cell-mono">${esc(d.node_name || '—')}</td>
      <td class="cell-muted">${esc(limits || 'No limits')}</td>
      <td class="num">${d.link_count || '—'}</td>
    </tr>`;
}

/** Render the databases table (or its empty state) into `wrap`. */
export function renderDatabasesTable(wrap, databases) {
  if (!databases.length) {
    wrap.innerHTML = `
      <div class="apps-empty">
        <strong>No databases yet</strong>
        Create PostgreSQL, MySQL, MariaDB, Redis, Valkey or MongoDB on any node and link it to your apps.
        <div style="margin-top:18px"><button class="btn" id="empty-new-database" data-perm="databases.create">New database</button></div>
      </div>`;
    document.getElementById('empty-new-database')?.addEventListener('click', openCreateDatabaseDialog);
  } else {
    wrap.innerHTML = `
      <table class="table apps-table">
        <thead><tr><th>Name</th><th>Engine</th><th>Node</th><th>Limits</th><th class="num">Linked apps</th></tr></thead>
        <tbody>${databases.map(rowHTML).join('')}</tbody>
      </table>`;
    wrap.querySelectorAll('tr[data-db-id]').forEach(tr => {
      tr.addEventListener('click', e => {
        if (e.target.closest('a')) return;
        location.href = `/database?id=${tr.dataset.dbId}`;
      });
    });
  }
  if (window._applyPermVisibility) window._applyPermVisibility(wrap);
}

export async function openCreateDatabaseDialog() {
  let nodes, existing;
  try {
    [engines, nodes, existing] = await Promise.all([
      engines.length ? engines : api.listDatabaseEngines(),
      api.listNodes(),
      api.listDatabases().catch(() => []),
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
    <div class="dialog dialog-modern form-dialog db-dialog" role="dialog" aria-modal="true" aria-labelledby="db-dlg-title">
      <div class="dialog-title" id="db-dlg-title">New database</div>
      <div class="dialog-body">
        <fieldset class="form-block">
          <legend>Engine</legend>
          <div class="engine-grid" role="radiogroup">
            ${engines.map((e, i) => `
              <label class="engine-card">
                <input type="radio" name="db-engine" value="${esc(e.key)}" ${i === 0 ? 'checked' : ''} class="sr-only" />
                ${engineIcon(e.key, 32)}
                <span class="engine-name">${esc(e.label)}</span>
                <span class="engine-kind">${esc(ENGINE_KIND[e.key] || '')} · ${e.port}</span>
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
        <div class="engine-preview">
          <span class="engine-preview-label">Linked apps get</span>
          <code id="db-url-preview"></code>
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

        <div class="db-mem-warning" id="db-mem-warning" style="display:none"></div>
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
    syncPreview();
  };
  const syncPreview = () => {
    const e = selectedEngine();
    if (!e) return;
    const ident = ($('#db-name').value.trim().toLowerCase() || 'shop-db').replace(/-/g, '_');
    const url = (URL_PREVIEW[e.key] || (() => ''))(ident, ident).replace('HOST', 'cloudbase-db-<id>');
    $('#db-url-preview').textContent = `${e.env_var}=${url}`;
  };
  const syncMemory = () => {
    const nodeId = parseInt(backdrop.querySelector('input[name="db-node"]:checked')?.value, 10);
    const text = memoryWarning(usableNodes.find(n => n.id === nodeId), existing, parseInt($('#db-mem').value, 10) || 0);
    const el = $('#db-mem-warning');
    el.textContent = text;
    el.style.display = text ? '' : 'none';
  };
  backdrop.querySelectorAll('input[name="db-node"]').forEach(r => r.addEventListener('change', syncMemory));
  $('#db-mem').addEventListener('input', syncMemory);
  backdrop.querySelectorAll('input[name="db-engine"]').forEach(r => r.addEventListener('change', () => { syncEngine(); syncMemory(); }));
  $('#db-name').addEventListener('input', syncPreview);
  syncEngine();
  syncMemory();
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
