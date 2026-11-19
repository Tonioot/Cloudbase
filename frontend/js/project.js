// A project: its apps and databases at a glance, and (for whoever may manage it) its shared settings and members.
import { api } from './api.js';
import { esc, statusColor } from './shell.js';
import { toast, confirm } from './utils.js';
import { openDeployModal } from './modal.js';
import { openCreateDatabaseDialog } from './databases.js';

const params = new URLSearchParams(location.search);
const PROJECT_ID = parseInt(params.get('id'), 10);
const root = () => document.getElementById('project-page');

const kpi = (label, value, sub = '') =>
  `<div class="kpi"><span class="kpi-label">${label}</span><span class="kpi-value"><strong>${value}</strong></span><span class="kpi-sub">${sub}</span></div>`;

function group(title, sub, body, extra = '') {
  return `<section class="settings-group ${extra}">
    <div class="settings-group-hd"><div class="settings-group-title">${title}</div><div class="settings-group-sub">${sub}</div></div>
    <div class="settings-group-bd">${body}</div>
  </section>`;
}

export async function initProject() {
  if (params.get('new')) return renderNew();
  if (!PROJECT_ID) { location.href = '/'; return; }
  let project;
  try {
    project = await api.getProject(PROJECT_ID);
  } catch (e) {
    root().innerHTML = `<div class="apps-empty">${esc(e.message)}</div>`;
    return;
  }
  render(project);
}

/* ─── A new project ────────────────────────────────────────────────────── */
function renderNew() {
  root().innerHTML = `
    <div class="page-title-row"><h1>New project</h1>
      <span class="page-title-meta">A group of apps and databases, with its own members and shared settings</span></div>
    <div class="pj-new">
      <div class="field"><label class="field-label" for="pj-name">Name</label>
        <input class="input" id="pj-name" maxlength="60" placeholder="e.g. Webshop" autocomplete="off" /></div>
      <div class="field"><label class="field-label" for="pj-desc">Description <span class="hint">optional</span></label>
        <input class="input" id="pj-desc" maxlength="300" placeholder="What runs in it" /></div>
      <p class="section-hint">The apps of a new project reach each other by name; you can turn that off in its settings.</p>
      <div class="pj-actions"><a class="btn" href="/">Cancel</a><button class="btn btn-primary" id="pj-create">Create project</button></div>
    </div>`;
  const create = document.getElementById('pj-create');
  document.getElementById('pj-name').focus();
  create.onclick = async () => {
    create.disabled = true;
    try {
      const p = await api.createProject({ name: document.getElementById('pj-name').value.trim(),
        description: document.getElementById('pj-desc').value.trim() || null });
      location.href = `/project?id=${p.id}`;
    } catch (e) {
      toast(e.message, 'error');
      create.disabled = false;
    }
  };
}

/* ─── A project ────────────────────────────────────────────────────────── */
function appRow(a) {
  const status = a.running ? 'running' : a.statuses.includes('error') || a.statuses.includes('crashed') ? 'error'
    : a.statuses.length ? a.statuses[0] : 'stopped';
  return `<tr>
    <td class="pj-name"><span class="dot" style="background:${statusColor(status)}"></span><a href="/app?id=${a.id}">${esc(a.name)}</a></td>
    <td class="cell-muted">${esc(a.kind || 'web service')}</td>
    <td class="pj-mono">${a.running} / ${a.instances}</td>
    <td class="cell-muted">${esc(a.domain || '—')}</td>
  </tr>`;
}

function databaseRow(d) {
  return `<tr>
    <td class="pj-name"><span class="dot" style="background:${statusColor(d.status)}"></span><a href="/database?id=${d.id}">${esc(d.name)}</a></td>
    <td class="cell-muted">${esc(d.engine)} ${esc(d.version)}</td>
    <td class="cell-muted">${esc(d.status)}</td>
  </tr>`;
}

function render(p) {
  if (p.is_default) p = { ...p, name: 'No project', description: 'Apps and databases that belong to no project' };
  document.title = `Cloudbase — ${p.name}`;
  const manage = p.can['projects.manage'];
  root().innerHTML = `
    <div class="page-title-row">
      <h1>${esc(p.name)}</h1>
      <span class="page-title-meta">${esc(p.description || 'Project')}</span>
      <div class="page-title-actions">
        ${p.can['databases.create'] ? '<button class="btn" id="pj-new-db">New database</button>' : ''}
        ${p.can['apps.deploy'] ? '<button class="btn btn-primary" id="pj-new-app">New app</button>' : ''}
      </div>
    </div>

    <section class="kpis" aria-label="At a glance">
      ${kpi('Apps', `${p.apps_running}<span> / ${p.apps}</span>`, 'running')}
      ${kpi('Databases', p.databases, p.databases === 1 ? 'database' : 'databases')}
      ${kpi('Shared variables', p.env_count, 'given to every app')}
      ${kpi('Apps reach each other', p.apps_connected ? 'On' : 'Off', p.apps_connected ? 'as &lt;NAME&gt;_URL' : 'only through links')}
    </section>

    <section class="section">
      <div class="section-head"><h2>Apps</h2></div>
      ${p.app_list.length ? `<table class="table pj-table"><thead><tr><th>App</th><th>Type</th><th>Running</th><th>Domain</th></tr></thead>
        <tbody>${p.app_list.map(appRow).join('')}</tbody></table>`
        : '<div class="apps-empty">No apps in this project yet.</div>'}
    </section>

    ${p.can['databases.view'] ? `<section class="section">
      <div class="section-head"><h2>Databases</h2></div>
      ${p.database_list.length ? `<table class="table pj-table"><thead><tr><th>Database</th><th>Engine</th><th>Status</th></tr></thead>
        <tbody>${p.database_list.map(databaseRow).join('')}</tbody></table>`
        : '<div class="apps-empty">No databases in this project yet.</div>'}
    </section>` : ''}

    ${manage && !p.is_default ? `<section class="section">
      <div class="section-head"><h2>Settings</h2></div>
      ${group('General', 'How the project is called in the panel.', `
        <div class="form-pair">
          <div class="field"><label class="field-label" for="pj-name">Name</label><input class="input" id="pj-name" maxlength="60" value="${esc(p.name)}" /></div>
          <div class="field"><label class="field-label" for="pj-desc">Description</label><input class="input" id="pj-desc" maxlength="300" value="${esc(p.description || '')}" /></div>
        </div>
        <button class="btn btn-sm" id="pj-save-general">Save</button>`)}
      ${group('Connected apps', 'Every app of the project gets the address of the others as an environment variable, named after the app: <code>BACKEND_URL</code> for an app called “backend”. A variable or link of the same name wins.', `
        <label class="toggle-row form-toggle">
          <span><span class="field-label">Apps in this project reach each other</span>
          <span class="field-hint">Takes effect when an app starts, restarts or deploys.</span></span>
          <span class="toggle"><input type="checkbox" id="pj-connected" ${p.apps_connected ? 'checked' : ''} /><span class="toggle-slider"></span></span>
        </label>`)}
      ${group('Shared variables', 'Given to every app of the project, also during the build. A variable of the app itself with the same name wins. Changes take effect at the next start or deploy.', `
        <div class="pj-env" id="pj-env"><div class="field-hint">Loading…</div></div>
        <div class="pj-env-actions"><button class="btn btn-sm" id="pj-env-add">Add variable</button><button class="btn btn-sm btn-primary" id="pj-env-save">Save variables</button></div>`)}
      <div id="pj-members-group"></div>
      ${p.is_default ? '' : group('Delete project', 'Only an empty project can be deleted: move or delete its apps and databases first.', `
        <button class="btn btn-danger btn-sm" id="pj-delete" ${p.apps || p.databases ? 'disabled' : ''}>Delete project</button>`, 'settings-group--danger')}
    </section>` : ''}`;

  document.getElementById('pj-new-app')?.addEventListener('click', () =>
    openDeployModal(app => { location.href = `/app?id=${app.id}`; }, { projectId: p.id }));
  document.getElementById('pj-new-db')?.addEventListener('click', () => openCreateDatabaseDialog({ projectId: p.id }));
  if (manage) wireSettings(p);
}

/* ─── Settings ─────────────────────────────────────────────────────────── */
function envRow(key = '', value = '') {
  return `<div class="pj-env-row">
    <input class="input input-mono" placeholder="NAME" value="${esc(key)}" spellcheck="false" />
    <input class="input input-mono" placeholder="value" value="${esc(value)}" spellcheck="false" />
    <button class="btn btn-sm btn-ghost pj-env-remove" type="button" aria-label="Remove">×</button>
  </div>`;
}

async function wireSettings(p) {
  document.getElementById('pj-save-general').onclick = async () => {
    try {
      await api.updateProject(p.id, { name: document.getElementById('pj-name').value.trim(),
        description: document.getElementById('pj-desc').value.trim() });
      toast('Project saved');
      setTimeout(() => location.reload(), 300);
    } catch (e) { toast(e.message, 'error'); }
  };
  document.getElementById('pj-connected').onchange = async e => {
    try {
      await api.updateProject(p.id, { apps_connected: e.target.checked });
      toast(e.target.checked ? 'Apps reach each other from their next start' : 'Apps no longer reach each other from their next start');
    } catch (err) { e.target.checked = !e.target.checked; toast(err.message, 'error'); }
  };

  const envEl = document.getElementById('pj-env');
  try {
    const { env } = await api.getProjectEnv(p.id);
    const entries = Object.entries(env);
    envEl.innerHTML = entries.length ? entries.map(([k, v]) => envRow(k, v)).join('') : envRow();
  } catch (e) { envEl.innerHTML = `<div class="field-hint">${esc(e.message)}</div>`; }
  envEl.addEventListener('click', e => {
    if (e.target.closest('.pj-env-remove')) e.target.closest('.pj-env-row').remove();
  });
  document.getElementById('pj-env-add').onclick = () => envEl.insertAdjacentHTML('beforeend', envRow());
  document.getElementById('pj-env-save').onclick = async () => {
    const env = {};
    envEl.querySelectorAll('.pj-env-row').forEach(row => {
      const [k, v] = row.querySelectorAll('input');
      if (k.value.trim()) env[k.value.trim()] = v.value;
    });
    try { await api.setProjectEnv(p.id, env); toast('Shared variables saved'); }
    catch (e) { toast(e.message, 'error'); }
  };

  document.getElementById('pj-delete')?.addEventListener('click', async () => {
    if (!await confirm('Delete project?', `“${esc(p.name)}” is removed. It has no apps or databases.`)) return;
    try { await api.deleteProject(p.id); location.href = '/'; }
    catch (e) { toast(e.message, 'error'); }
  });

  renderMembers(p);
}

/* ─── Members (the admin account only) ─────────────────────────────────── */
async function renderMembers(p) {
  const holder = document.getElementById('pj-members-group');
  let members, users, roles;
  try {
    [members, users, roles] = await Promise.all([api.getProjectMembers(p.id), api.listUsers(), api.listRoles()]);
  } catch {
    return;                                      // not the admin account: members are managed there
  }
  const others = users.filter(u => u.username !== 'admin');
  const roleOptions = selected => roles.map(r => `<option value="${r.id}" ${r.id === selected ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
  const row = m => `<div class="pj-member" data-user="${m.user_id}">
      <span class="pj-member-name">${esc(m.username)}</span>
      <select class="input">${roleOptions(m.role_id)}</select>
      <button class="btn btn-sm btn-ghost pj-member-remove" type="button" aria-label="Remove">×</button>
    </div>`;
  holder.innerHTML = group('Members',
    'Someone added here sees only the projects they are a member of, each with the role given there. Users who are a member of no project see every project with their own role.', `
    <div class="pj-members" id="pj-members">${members.map(row).join('') || '<div class="field-hint" id="pj-members-empty">No members: everyone sees this project with their own role.</div>'}</div>
    <div class="pj-member-add">
      <select class="input" id="pj-add-user"><option value="">Add a user…</option>${others.map(u => `<option value="${u.id}">${esc(u.username)}</option>`).join('')}</select>
      <select class="input" id="pj-add-role">${roleOptions(roles[0]?.id)}</select>
      <button class="btn btn-sm" id="pj-add-member">Add</button>
    </div>
    <button class="btn btn-sm btn-primary" id="pj-members-save">Save members</button>`);

  const list = document.getElementById('pj-members');
  list.addEventListener('click', e => { if (e.target.closest('.pj-member-remove')) e.target.closest('.pj-member').remove(); });
  document.getElementById('pj-add-member').onclick = () => {
    const userId = parseInt(document.getElementById('pj-add-user').value, 10);
    if (!userId || list.querySelector(`[data-user="${userId}"]`)) return;
    document.getElementById('pj-members-empty')?.remove();
    const user = others.find(u => u.id === userId);
    list.insertAdjacentHTML('beforeend', row({ user_id: userId, username: user.username,
      role_id: parseInt(document.getElementById('pj-add-role').value, 10) }));
  };
  document.getElementById('pj-members-save').onclick = async () => {
    const payload = [...list.querySelectorAll('.pj-member')].map(el => ({
      user_id: parseInt(el.dataset.user, 10), role_id: parseInt(el.querySelector('select').value, 10) }));
    try { await api.setProjectMembers(p.id, payload); toast('Members saved'); }
    catch (e) { toast(e.message, 'error'); }
  };
}
