// Settings pages: /settings?s=domain|system|users|tokens|transfer|account
//
// Each section is a full page (title, description, grouped forms) instead of
// the modals these used to be. The sidebar links here and marks the active one.

import { api } from './api.js';
import { setCrumbs } from './shell.js';
import { toast, confirm } from './utils.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const SETTINGS_SECTIONS = {
  domain:   { title: 'Domain & SSL',     sub: 'Where the panel and your apps are reachable, and the certificates nginx serves them with.' },
  system:   { title: 'System settings',  sub: 'Session length, port ranges and resource limits for this Cloudbase installation.' },
  users:    { title: 'Users & roles',    sub: 'Who can sign in to this panel, and what each role is allowed to do.' },
  tokens:   { title: 'GitHub tokens',    sub: 'Saved access tokens for private repositories. Pick one when you create or edit an app.' },
  transfer: { title: 'Export / import',  sub: 'Move app configurations between Cloudbase installations, or keep a backup.' },
  account:  { title: 'Account',          sub: 'Your own sign-in details.' },
};

const RENDER = {
  domain: renderDomain,
  system: renderSystem,
  users: renderUsers,
  tokens: renderTokens,
  transfer: renderTransfer,
  account: renderAccount,
};

export function initSettingsPage() {
  const key = new URLSearchParams(location.search).get('s') || 'domain';
  const section = SETTINGS_SECTIONS[key] ? key : 'domain';
  const meta = SETTINGS_SECTIONS[section];

  document.title = `${meta.title} — Cloudbase`;
  document.getElementById('settings-title').textContent = meta.title;
  document.getElementById('settings-sub').textContent = meta.sub;
  setCrumbs(['Settings', meta.title]);

  const root = document.getElementById('settings-root');
  root.innerHTML = '';
  RENDER[section](root);
}

/* ─── Shared bits ───────────────────────────────────────────────────────── */

function group(title, sub, body, extraClass = '') {
  return `
    <section class="settings-group ${extraClass}">
      <div class="settings-group-hd">
        <div class="settings-group-title">${title}</div>
        <div class="settings-group-sub">${sub}</div>
      </div>
      <div class="settings-group-bd">${body}</div>
    </section>`;
}

function actionBar(label, buttonId, buttonText) {
  return `
    <div class="settings-bar">
      <span class="settings-bar-title">${label}</span>
      <div class="settings-bar-actions">
        <span class="settings-status" id="settings-status" role="status"></span>
        <button class="btn btn-primary btn-sm" id="${buttonId}">${buttonText}</button>
      </div>
    </div>`;
}

function setStatus(text, tone = '') {
  const el = document.getElementById('settings-status');
  if (!el) return;
  el.textContent = text;
  el.dataset.tone = tone;
}

function notice(text, tone = 'error') {
  return `<div class="settings-notice" data-tone="${tone}">${text}</div>`;
}

function certField(id, label, hint, accept) {
  return `
    <div class="field">
      <label class="field-label">${label} <span class="hint">optional</span></label>
      <div class="cert-upload-row">
        <span class="cert-filename" id="${id}-name">No file selected</span>
        <button type="button" class="btn btn-sm" id="${id}-upload">Upload</button>
        <button type="button" class="btn btn-sm btn-ghost" id="${id}-clear" title="Remove">Clear</button>
        <input type="file" id="${id}-file" accept="${accept}" hidden>
        <input type="hidden" id="${id}">
      </div>
      <div class="field-hint">${hint}</div>
    </div>`;
}

function setCert(id, path) {
  document.getElementById(id).value = path || '';
  const name = document.getElementById(`${id}-name`);
  name.textContent = path ? path.split('/').pop() : 'No file selected';
  name.title = path || '';
  name.classList.toggle('has-value', !!path);
}

function wireCert(id) {
  const fileInput = document.getElementById(`${id}-file`);
  const upload = document.getElementById(`${id}-upload`);
  upload.onclick = () => fileInput.click();
  document.getElementById(`${id}-clear`).onclick = () => setCert(id, '');
  fileInput.onchange = async e => {
    const file = e.target.files[0];
    if (!file) return;
    upload.disabled = true;
    try {
      const res = await api.uploadSystemCert(file);
      setCert(id, res.path);
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      upload.disabled = false;
      e.target.value = '';
    }
  };
}

/* ─── Domain & SSL ──────────────────────────────────────────────────────── */

function renderDomain(root) {
  root.innerHTML = `
    ${actionBar('Changes are written to nginx and reloaded when you save.', 'domain-save', 'Save &amp; apply')}
    ${group('Cloudbase panel',
      'The address of this dashboard. nginx listens on 80/443 and forwards to Cloudbase on port 7823.',
      `<div class="field">
         <label class="field-label" for="pdm-domain">Domain</label>
         <input class="input" id="pdm-domain" placeholder="panel.example.com" autocomplete="off" />
         <div class="field-hint">Point an A record for this name at the server first. Leave the certificate empty to serve over plain HTTP.</div>
       </div>
       ${certField('pdm-cert', 'SSL certificate', 'Full chain in PEM format (<code>fullchain.pem</code>, <code>.crt</code>).', '.pem,.crt,.cer')}
       ${certField('pdm-key', 'SSL private key', 'The matching private key (<code>privkey.pem</code>, <code>.key</code>).', '.pem,.key')}`)}
    ${group('App subdomains',
      'Give every app an automatic address like <code>myapp.apps.example.com</code>, before or instead of a custom domain.',
      `<div class="field">
         <label class="field-label" for="pdm-base-domain">Base domain <span class="hint">optional</span></label>
         <input class="input" id="pdm-base-domain" placeholder="apps.example.com" autocomplete="off" />
         <div class="field-hint">Needs a wildcard DNS record: <code>*.apps.example.com → server IP</code>. Leave empty to turn automatic subdomains off.</div>
       </div>
       ${certField('pdm-base-cert', 'Wildcard certificate', 'A certificate for <code>*.apps.example.com</code>.', '.pem,.crt,.cer')}
       ${certField('pdm-base-key', 'Wildcard private key', 'The key that belongs to the wildcard certificate.', '.pem,.key')}`)}
    ${group('Status pages',
      'Saving also regenerates the page nginx shows while Cloudbase restarts, and the page for hostnames that aren’t linked to an app.',
      `<div class="settings-note">Nothing to configure here — these follow the panel domain above.</div>`)}
  `;

  ['pdm-cert', 'pdm-key', 'pdm-base-cert', 'pdm-base-key'].forEach(wireCert);

  api.getPDManagerNginx().then(data => {
    if (data.exists && data.content) {
      const m = data.content.match(/server_name\s+([^\s;]+)/);
      if (m) document.getElementById('pdm-domain').value = m[1];
      const c = data.content.match(/ssl_certificate\s+([^\s;]+)/);
      if (c) setCert('pdm-cert', c[1]);
      const k = data.content.match(/ssl_certificate_key\s+([^\s;]+)/);
      if (k) setCert('pdm-key', k[1]);
    }
    if (data.base_domain) document.getElementById('pdm-base-domain').value = data.base_domain;
    if (data.base_ssl_cert_path) setCert('pdm-base-cert', data.base_ssl_cert_path);
    if (data.base_ssl_key_path) setCert('pdm-base-key', data.base_ssl_key_path);
  }).catch(() => {});

  const save = document.getElementById('domain-save');
  save.onclick = async () => {
    const val = id => document.getElementById(id).value.trim() || null;
    const domain = val('pdm-domain');
    if (!domain) { setStatus('Enter the panel domain first.', 'error'); document.getElementById('pdm-domain').focus(); return; }
    save.disabled = true;
    setStatus('Applying…');
    try {
      const res = await api.applyPDManagerNginx({
        domain,
        ssl_cert_path: val('pdm-cert'),
        ssl_key_path: val('pdm-key'),
        base_domain: val('pdm-base-domain'),
        base_ssl_cert_path: val('pdm-base-cert'),
        base_ssl_key_path: val('pdm-base-key'),
      });
      if (res.ok) {
        const proto = val('pdm-cert') ? 'https' : 'http';
        setStatus(`Applied — panel at ${proto}://${domain}`, 'ok');
      } else {
        setStatus(res.message || 'nginx rejected the configuration', 'error');
      }
    } catch (e) {
      setStatus(e.message, 'error');
    } finally {
      save.disabled = false;
    }
  };
}

/* ─── System settings ───────────────────────────────────────────────────── */

const SYS_FIELDS = [
  ['auth', 'token_expire_seconds', 'sys-token-expire'],
  ['ports', 'instance_min', 'sys-inst-min'], ['ports', 'instance_max', 'sys-inst-max'],
  ['ports', 'tunnel_min', 'sys-tun-min'], ['ports', 'tunnel_max', 'sys-tun-max'],
  ['limits', 'max_apps', 'sys-max-apps'], ['limits', 'max_instances', 'sys-max-inst'],
  ['limits', 'max_nodes', 'sys-max-nodes'],
  ['limits', 'max_restarts_per_window', 'sys-max-restarts'], ['limits', 'restart_window_seconds', 'sys-restart-window'],
];

function numField(id, label, hint, attrs = '') {
  return `
    <div class="field">
      <label class="field-label" for="${id}">${label}</label>
      <input class="input" id="${id}" type="number" ${attrs} />
      ${hint ? `<div class="field-hint">${hint}</div>` : ''}
    </div>`;
}

function renderSystem(root) {
  root.innerHTML = `
    ${actionBar('Stored in <code>~/.cloudbase/config.yaml</code>.', 'system-save', 'Save')}
    ${group('Sessions', 'How long a sign-in stays valid before you have to log in again.',
      `<div class="form-pair">${numField('sys-token-expire', 'Session length (seconds)', '3600 = one hour. Applies to new sign-ins.', 'min="60" max="2592000"')}</div>`)}
    ${group('Instance ports', 'Host ports handed out to instances. Each instance gets one free port from this range.',
      `<div class="form-pair">
        ${numField('sys-inst-min', 'From', '', 'min="1024" max="65000"')}
        ${numField('sys-inst-max', 'To', '', 'min="1024" max="65000"')}
      </div>
      <div class="field-hint">Takes effect after a Cloudbase restart. Keep the range free of other services.</div>`)}
    ${group('Tunnel ports', 'Local ports on the primary where traffic for instances on other nodes arrives through their tunnel.',
      `<div class="form-pair">
        ${numField('sys-tun-min', 'From', '', 'min="1024" max="65000"')}
        ${numField('sys-tun-max', 'To', '', 'min="1024" max="65000"')}
      </div>
      <div class="field-hint">Only bound on <code>127.0.0.1</code>, so they don't need to be open in your firewall. Takes effect after a restart.</div>`)}
    ${group('Limits', 'Upper bounds that protect the server from runaway scaling.',
      `<div class="form-pair">
        ${numField('sys-max-apps', 'Max apps', '', 'min="1"')}
        ${numField('sys-max-inst', 'Max instances', 'Across all apps.', 'min="1"')}
      </div>
      <div class="form-pair">${numField('sys-max-nodes', 'Max nodes', '', 'min="1"')}</div>`)}
    ${group('Crash protection', 'When an instance keeps crashing, Cloudbase stops restarting it and marks it as failed.',
      `<div class="form-pair">
        ${numField('sys-max-restarts', 'Max restarts', 'Within one window.', 'min="1"')}
        ${numField('sys-restart-window', 'Window (seconds)', '', 'min="10"')}
      </div>`)}
  `;

  api.getSystemSettings().then(data => {
    for (const [sec, key, id] of SYS_FIELDS) document.getElementById(id).value = data[sec]?.[key] ?? '';
  }).catch(e => setStatus(e.message, 'error'));

  const save = document.getElementById('system-save');
  save.onclick = async () => {
    const body = { auth: {}, ports: {}, limits: {} };
    for (const [sec, key, id] of SYS_FIELDS) body[sec][key] = parseInt(document.getElementById(id).value, 10);
    save.disabled = true;
    setStatus('Saving…');
    try {
      await api.saveSystemSettings(body);
      setStatus('Saved. Port changes apply after a restart.', 'ok');
    } catch (e) {
      setStatus(e.message || 'Failed to save', 'error');
    } finally {
      save.disabled = false;
    }
  };
}

/* ─── Users & roles ─────────────────────────────────────────────────────── */

const BUILT_IN_ROLES = new Set(['Administrator', 'Viewer']);

function currentPerms() {
  try { return new Set(JSON.parse(document.body.dataset.permissions || '[]')); } catch { return new Set(); }
}
const isRoot = () => document.body.dataset.role === 'Root';
const canUsers = () => isRoot() || currentPerms().has('users.manage');
const canRoles = () => isRoot() || currentPerms().has('roles.manage');

function renderUsers(root) {
  if (!canUsers() && !canRoles()) {
    root.innerHTML = notice('You don’t have permission to manage users or roles.');
    return;
  }
  root.innerHTML = `
    ${canUsers() ? group('Users', 'Everyone who can sign in. The built-in <code>admin</code> account is Root and always has every permission.',
      `<div class="settings-list-head">
         <span id="users-count" class="settings-count"></span>
         ${canRoles() ? '<button class="btn btn-sm" id="user-new">New user</button>' : ''}
       </div>
       <div id="users-list"></div>`) : ''}
    ${canRoles() ? group('Roles', 'A role is a named set of permissions. Administrator and Viewer are built in and can’t be changed.',
      `<div class="settings-list-head">
         <span id="roles-count" class="settings-count"></span>
         <button class="btn btn-sm" id="role-new">New role</button>
       </div>
       <div id="roles-list"></div>`) : ''}
  `;
  document.getElementById('user-new')?.addEventListener('click', () => openUserDialog());
  document.getElementById('role-new')?.addEventListener('click', () => openRoleDialog());
  if (canUsers()) loadUsers();
  if (canRoles()) loadRoles();
}

async function loadUsers() {
  const list = document.getElementById('users-list');
  list.innerHTML = '<div class="settings-note">Loading…</div>';
  try {
    const users = await api.listUsers();
    document.getElementById('users-count').textContent = `${users.length} user${users.length === 1 ? '' : 's'}`;
    list.innerHTML = `
      <table class="table settings-table">
        <thead><tr><th>User</th><th>Role</th><th>Created</th><th></th></tr></thead>
        <tbody>${users.map(u => {
          const root = !!u.is_root || u.username === 'admin';
          return `<tr data-id="${u.id}">
            <td><span class="user-cell"><span class="rail-avatar">${esc(u.username.slice(0, 2).toUpperCase())}</span>${esc(u.username)}</span></td>
            <td>${root ? '<span class="pill">Root</span>' : esc(u.role || '—')}</td>
            <td class="cell-muted">${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
            <td class="row-actions">${root ? '' : `
              ${canRoles() ? '<button class="btn btn-sm btn-ghost" data-act="edit">Edit</button>' : ''}
              <button class="btn btn-sm btn-ghost btn-danger-text" data-act="delete">Delete</button>`}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
    list.querySelectorAll('tr[data-id]').forEach(tr => {
      const u = users.find(x => String(x.id) === tr.dataset.id);
      tr.querySelector('[data-act="edit"]')?.addEventListener('click', () => openUserDialog(u));
      tr.querySelector('[data-act="delete"]')?.addEventListener('click', async () => {
        if (!await confirm(`Delete "${u.username}"?`, 'They will be signed out and can no longer log in.')) return;
        try { await api.deleteUser(u.id); toast(`User "${u.username}" deleted`); loadUsers(); }
        catch (e) { toast(e.message, 'error'); }
      });
    });
  } catch (e) {
    list.innerHTML = notice(esc(e.message));
  }
}

async function loadRoles() {
  const list = document.getElementById('roles-list');
  list.innerHTML = '<div class="settings-note">Loading…</div>';
  try {
    const [roles, perms] = await Promise.all([api.listRoles(), api.listPermissions()]);
    document.getElementById('roles-count').textContent = `${roles.length} role${roles.length === 1 ? '' : 's'}`;
    list.innerHTML = `
      <table class="table settings-table">
        <thead><tr><th>Role</th><th>Permissions</th><th></th></tr></thead>
        <tbody>${roles.map(r => {
          const builtIn = BUILT_IN_ROLES.has(r.name);
          return `<tr data-id="${r.id}" class="clickable">
            <td>
              <div class="role-name">${esc(r.name)}${builtIn ? ' <span class="pill">built-in</span>' : ''}</div>
              ${r.description ? `<div class="cell-muted role-desc">${esc(r.description)}</div>` : ''}
            </td>
            <td class="cell-muted">${r.permissions.length} of ${perms.length}</td>
            <td class="row-actions">
              <button class="btn btn-sm btn-ghost" data-act="edit">${builtIn ? 'View' : 'Edit'}</button>
              ${builtIn ? '' : '<button class="btn btn-sm btn-ghost btn-danger-text" data-act="delete">Delete</button>'}
            </td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
    list.querySelectorAll('tr[data-id]').forEach(tr => {
      const r = roles.find(x => String(x.id) === tr.dataset.id);
      tr.addEventListener('click', e => { if (!e.target.closest('[data-act="delete"]')) openRoleDialog(r, perms); });
      tr.querySelector('[data-act="delete"]')?.addEventListener('click', async e => {
        e.stopPropagation();
        if (!await confirm(`Delete role "${r.name}"?`, 'Users with this role lose its permissions.')) return;
        try { await api.deleteRole(r.id); toast(`Role "${r.name}" deleted`); loadRoles(); }
        catch (err) { toast(err.message, 'error'); }
      });
    });
  } catch (e) {
    list.innerHTML = notice(esc(e.message));
  }
}

function openDialog(title, bodyHTML, { saveLabel = 'Save', readOnly = false, wide = false } = {}) {
  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop';
  backdrop.style.display = 'flex';
  backdrop.innerHTML = `
    <div class="dialog dialog-modern form-dialog${wide ? ' form-dialog--wide' : ''}" role="dialog" aria-modal="true">
      <div class="dialog-title">${title}</div>
      <div class="dialog-body">${bodyHTML}<div class="dialog-error" hidden></div></div>
      <div class="dialog-actions">
        <button class="btn" data-close>${readOnly ? 'Close' : 'Cancel'}</button>
        ${readOnly ? '' : `<button class="btn btn-primary" data-save>${saveLabel}</button>`}
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  backdrop.querySelector('[data-close]').onclick = close;
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  backdrop.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  const errEl = backdrop.querySelector('.dialog-error');
  return {
    el: backdrop,
    close,
    saveBtn: backdrop.querySelector('[data-save]'),
    error(msg) { errEl.textContent = msg; errEl.hidden = !msg; },
  };
}

async function openUserDialog(user = null) {
  let roles;
  try { roles = await api.listRoles(); } catch (e) { toast(e.message, 'error'); return; }
  const edit = !!user;
  const dlg = openDialog(edit ? `Edit ${esc(user.username)}` : 'New user', `
    <div class="field">
      <label class="field-label" for="u-name">Username</label>
      <input class="input" id="u-name" value="${edit ? esc(user.username) : ''}" placeholder="jane" autocomplete="off" />
    </div>
    <div class="field">
      <label class="field-label" for="u-pwd">${edit ? 'New password' : 'Password'} <span class="hint">${edit ? 'leave empty to keep' : 'at least 8 characters'}</span></label>
      <input class="input input-mono" id="u-pwd" type="password" autocomplete="new-password" />
    </div>
    <div class="field">
      <label class="field-label" for="u-role">Role</label>
      <select class="input" id="u-role">
        ${roles.map(r => `<option value="${r.id}" ${edit && r.id === user.role_id ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}
      </select>
    </div>`, { saveLabel: edit ? 'Save' : 'Create user' });

  const $ = id => dlg.el.querySelector(id);
  setTimeout(() => $('#u-name').focus(), 30);
  dlg.saveBtn.onclick = async () => {
    const username = $('#u-name').value.trim();
    const pwd = $('#u-pwd').value;
    const roleId = parseInt($('#u-role').value, 10);
    if (username.length < 2) return dlg.error('Username must be at least 2 characters.');
    if ((!edit || pwd) && pwd.length < 8) return dlg.error('Password must be at least 8 characters.');
    dlg.saveBtn.disabled = true;
    try {
      if (edit) {
        const payload = { role_id: roleId };
        if (username !== user.username) payload.username = username;
        if (pwd) payload.password = pwd;
        await api.updateUser(user.id, payload);
        toast(`User "${username}" updated`);
      } else {
        await api.createUser({ username, password: pwd, role_id: roleId });
        toast(`User "${username}" created`);
      }
      dlg.close();
      loadUsers();
    } catch (e) {
      dlg.error(e.message);
      dlg.saveBtn.disabled = false;
    }
  };
}

const PERM_GROUP_LABELS = { apps: 'Apps', nodes: 'Nodes', system: 'System', users: 'Users', roles: 'Roles', audit: 'Audit log', tokens: 'GitHub tokens' };

async function openRoleDialog(role = null, perms = null) {
  if (!perms) { try { perms = await api.listPermissions(); } catch (e) { toast(e.message, 'error'); return; } }
  const edit = !!role;
  const builtIn = edit && BUILT_IN_ROLES.has(role.name);
  const active = new Set(edit ? role.permissions.map(p => p.id) : []);

  const groups = {};
  for (const p of perms) (groups[p.name.split('.')[0]] ||= []).push(p);

  const dlg = openDialog(edit ? (builtIn ? esc(role.name) : `Edit ${esc(role.name)}`) : 'New role', `
    ${builtIn ? '<div class="form-dialog-intro">Built-in role — its permissions can’t be changed.</div>' : `
    <div class="field">
      <label class="field-label" for="r-name">Name</label>
      <input class="input" id="r-name" value="${edit ? esc(role.name) : ''}" placeholder="developer" autocomplete="off" />
    </div>
    <div class="field">
      <label class="field-label" for="r-desc">Description <span class="hint">optional</span></label>
      <input class="input" id="r-desc" value="${edit ? esc(role.description || '') : ''}" placeholder="Can deploy and restart apps" />
    </div>`}
    <div class="perm-groups">
      ${Object.entries(groups).map(([g, list]) => `
        <fieldset class="perm-group">
          <legend>${esc(PERM_GROUP_LABELS[g] || g)}</legend>
          ${list.map(p => `
            <label class="perm-check">
              <input type="checkbox" value="${p.id}" ${active.has(p.id) ? 'checked' : ''} ${builtIn ? 'disabled' : ''} />
              <span><span class="perm-name">${esc(p.name)}</span>${p.description ? `<span class="perm-desc">${esc(p.description)}</span>` : ''}</span>
            </label>`).join('')}
        </fieldset>`).join('')}
    </div>`, { saveLabel: edit ? 'Save' : 'Create role', readOnly: builtIn, wide: true });

  if (builtIn) return;
  const $ = id => dlg.el.querySelector(id);
  setTimeout(() => $('#r-name').focus(), 30);
  dlg.saveBtn.onclick = async () => {
    const name = $('#r-name').value.trim();
    if (name.length < 2) return dlg.error('Role name must be at least 2 characters.');
    const payload = {
      name,
      description: $('#r-desc').value.trim() || null,
      permission_ids: [...dlg.el.querySelectorAll('.perm-check input:checked')].map(i => parseInt(i.value, 10)),
    };
    dlg.saveBtn.disabled = true;
    try {
      if (edit) await api.updateRole(role.id, payload); else await api.createRole(payload);
      toast(edit ? 'Role updated' : `Role "${name}" created`);
      dlg.close();
      loadRoles();
    } catch (e) {
      dlg.error(e.message);
      dlg.saveBtn.disabled = false;
    }
  };
}

/* ─── GitHub tokens ─────────────────────────────────────────────────────── */

function renderTokens(root) {
  root.innerHTML = `
    ${group('Saved tokens', 'Tokens are stored encrypted. Only the last four characters are ever shown again.',
      `<div id="tokens-list"></div>`)}
    ${group('Add a token', 'Use a fine-grained token with read access to <strong>Contents</strong> for the repositories you deploy. A classic token needs the <code>repo</code> scope.',
      `<div class="field">
         <label class="field-label" for="tok-label">Label</label>
         <input class="input" id="tok-label" placeholder="my-org" autocomplete="off" />
         <div class="field-hint">How it shows up in the token picker.</div>
       </div>
       <div class="field">
         <label class="field-label" for="tok-value">Token</label>
         <input class="input input-mono" id="tok-value" type="password" placeholder="github_pat_… or ghp_…" autocomplete="off" />
       </div>
       <div class="field"><button class="btn btn-primary btn-sm" id="tok-add">Save token</button></div>`)}
  `;
  loadTokens();

  document.getElementById('tok-add').onclick = async () => {
    const label = document.getElementById('tok-label').value.trim();
    const token = document.getElementById('tok-value').value.trim();
    if (!label) return toast('Give the token a label', 'error');
    if (!token) return toast('Paste the token first', 'error');
    try {
      await api.saveGitHubToken(label, token);
      document.getElementById('tok-label').value = '';
      document.getElementById('tok-value').value = '';
      toast(`Token "${label}" saved`);
      loadTokens();
    } catch (e) {
      toast(e.message, 'error');
    }
  };
}

async function loadTokens() {
  const list = document.getElementById('tokens-list');
  list.innerHTML = '<div class="settings-note">Loading…</div>';
  try {
    const tokens = await api.listGitHubTokens();
    if (!tokens.length) {
      list.innerHTML = '<div class="settings-note">No tokens saved yet.</div>';
      return;
    }
    list.innerHTML = `
      <table class="table settings-table">
        <thead><tr><th>Label</th><th>Token</th><th></th></tr></thead>
        <tbody>${tokens.map(t => `
          <tr data-id="${t.id}">
            <td>${esc(t.label)}</td>
            <td class="cell-mono cell-muted">••••${esc(t.token_hint)}</td>
            <td class="row-actions"><button class="btn btn-sm btn-ghost btn-danger-text" data-act="delete">Delete</button></td>
          </tr>`).join('')}</tbody>
      </table>`;
    list.querySelectorAll('[data-act="delete"]').forEach(btn => {
      btn.onclick = async () => {
        const tr = btn.closest('tr');
        const t = tokens.find(x => String(x.id) === tr.dataset.id);
        if (!await confirm(`Delete token "${t.label}"?`, 'Apps that use it keep working until their next pull.')) return;
        try { await api.deleteGitHubToken(t.id); toast('Token deleted'); loadTokens(); }
        catch (e) { toast(e.message, 'error'); }
      };
    });
  } catch (e) {
    list.innerHTML = notice(esc(e.message));
  }
}

/* ─── Export / import ───────────────────────────────────────────────────── */

function renderTransfer(root) {
  root.innerHTML = `
    ${group('Export', 'Downloads a JSON file with the configuration of the selected apps: repository, commands, port, domains, Docker limits and maintenance pages.',
      `<div class="settings-list-head">
         <label class="check-inline"><input type="checkbox" id="exp-all" checked /> Select all</label>
         <button class="btn btn-primary btn-sm" id="exp-go">Download JSON</button>
       </div>
       <div id="exp-list" class="check-list"></div>
       <div class="field-hint">Environment variables and GitHub tokens are left out on purpose — add them again after importing.</div>`)}
    ${group('Import', 'Creates each app with one instance and deploys it from its repository. Apps whose name already exists are skipped.',
      `<div class="field">
         <label class="field-label">Export file</label>
         <div class="cert-upload-row">
           <span class="cert-filename" id="imp-name">No file selected</span>
           <button type="button" class="btn btn-sm" id="imp-browse">Choose file</button>
           <input type="file" id="imp-file" accept=".json,application/json" hidden>
         </div>
         <div class="field-hint" id="imp-summary"></div>
       </div>
       <div class="field">
         <label class="field-label" for="imp-node">Target node</label>
         <select class="input" id="imp-node"><option value="">Same node as in the export</option></select>
         <div class="field-hint">Override this to put every imported app on one node.</div>
       </div>
       <div class="field"><button class="btn btn-primary btn-sm" id="imp-go" disabled>Import apps</button></div>`)}
  `;

  // Export
  const expList = document.getElementById('exp-list');
  const expAll = document.getElementById('exp-all');
  api.listApps().then(apps => {
    if (!apps.length) { expList.innerHTML = '<div class="settings-note">No apps to export.</div>'; return; }
    expList.innerHTML = apps.map(a => `
      <label class="check-row">
        <input type="checkbox" class="exp-check" value="${a.id}" checked />
        <span class="check-row-name">${esc(a.name)}</span>
        <span class="check-row-meta">${(a.replicas || []).length} instance${(a.replicas || []).length === 1 ? '' : 's'}</span>
      </label>`).join('');
    const checks = [...expList.querySelectorAll('.exp-check')];
    checks.forEach(c => c.onchange = () => { expAll.checked = checks.every(x => x.checked); });
  }).catch(e => { expList.innerHTML = notice(esc(e.message)); });
  expAll.onchange = () => expList.querySelectorAll('.exp-check').forEach(c => { c.checked = expAll.checked; });

  const expGo = document.getElementById('exp-go');
  expGo.onclick = async () => {
    const checks = [...expList.querySelectorAll('.exp-check')];
    const ids = checks.filter(c => c.checked).map(c => parseInt(c.value, 10));
    if (checks.length && !ids.length) return toast('Select at least one app', 'error');
    expGo.disabled = true;
    try {
      const res = await api.exportApps(ids.length === checks.length ? null : ids);
      const blob = new Blob([JSON.stringify(res.exported_apps, null, 2)], { type: 'application/json' });
      const a = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(blob),
        download: `cloudbase_apps_${new Date().toISOString().slice(0, 10)}.json`,
      });
      a.click();
      URL.revokeObjectURL(a.href);
      toast(`Exported ${res.exported_apps.length} app${res.exported_apps.length === 1 ? '' : 's'}`);
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      expGo.disabled = false;
    }
  };

  // Import
  let data = null;
  const fileInput = document.getElementById('imp-file');
  const summary = document.getElementById('imp-summary');
  const impGo = document.getElementById('imp-go');
  document.getElementById('imp-browse').onclick = () => fileInput.click();
  fileInput.onchange = () => {
    const file = fileInput.files[0];
    if (!file) return;
    const name = document.getElementById('imp-name');
    name.textContent = file.name;
    name.classList.add('has-value');
    file.text().then(text => {
      try {
        data = JSON.parse(text);
        if (!Array.isArray(data)) throw new Error('expected a list of apps');
        summary.textContent = `${data.length} app${data.length === 1 ? '' : 's'}: ${data.map(a => a.name).filter(Boolean).join(', ')}`;
        summary.dataset.tone = '';
        impGo.disabled = !data.length;
      } catch (e) {
        data = null;
        summary.textContent = `This isn't a Cloudbase export (${e.message}).`;
        summary.dataset.tone = 'error';
        impGo.disabled = true;
      }
    });
  };
  api.listNodes().then(nodes => {
    const sel = document.getElementById('imp-node');
    nodes.forEach(n => sel.insertAdjacentHTML('beforeend', `<option value="${n.id}">${esc(n.is_local ? `${n.name} (primary)` : n.name)}</option>`));
  }).catch(() => {});
  impGo.onclick = async () => {
    if (!data) return;
    const nodeVal = document.getElementById('imp-node').value;
    impGo.disabled = true;
    summary.textContent = 'Importing…';
    try {
      await api.importApps(data, nodeVal ? parseInt(nodeVal, 10) : null);
      toast('Apps imported');
      summary.textContent = 'Imported. The apps are deploying — follow them from the overview.';
    } catch (e) {
      summary.textContent = `Import failed: ${e.message}`;
      summary.dataset.tone = 'error';
      impGo.disabled = false;
    }
  };
}

/* ─── Account ───────────────────────────────────────────────────────────── */

function renderAccount(root) {
  root.innerHTML = `
    ${group('Signed in as', 'Your username and role on this panel.',
      `<div class="info-row"><span class="label">Username</span><span class="value" id="acc-user">—</span></div>
       <div class="info-row"><span class="label">Role</span><span class="value" id="acc-role">—</span></div>
       <div class="info-row"><span class="label">Session</span><span class="value" id="acc-session">—</span></div>`)}
    ${group('Change password', 'You’ll be signed out afterwards and can log in with the new password.',
      `<form id="pwd-form" autocomplete="on" onsubmit="return false">
         <input type="text" autocomplete="username" hidden />
         <div class="field">
           <label class="field-label" for="pwd-new">New password <span class="hint">at least 8 characters</span></label>
           <input class="input input-mono" id="pwd-new" type="password" autocomplete="new-password" />
         </div>
         <div class="field">
           <label class="field-label" for="pwd-confirm">Confirm password</label>
           <input class="input input-mono" id="pwd-confirm" type="password" autocomplete="new-password" />
         </div>
         <div class="field-hint settings-inline-status" id="pwd-status"></div>
         <div class="field"><button class="btn btn-primary btn-sm" id="pwd-save" type="submit">Update password</button></div>
       </form>`)}
  `;

  api.checkAuth().then(d => {
    document.getElementById('acc-user').textContent = d.username || '—';
    document.getElementById('acc-role').textContent = d.is_root || d.is_superadmin ? 'Root' : (d.role || '—');
  }).catch(() => {});
  api.getSession().then(s => {
    const m = Math.round((s.expires_in || 0) / 60);
    document.getElementById('acc-session').textContent = `Expires in ${m} min`;
  }).catch(() => {});

  const status = document.getElementById('pwd-status');
  document.getElementById('pwd-form').onsubmit = async e => {
    e.preventDefault();
    const pwd = document.getElementById('pwd-new').value;
    const pwd2 = document.getElementById('pwd-confirm').value;
    status.dataset.tone = 'error';
    if (pwd.length < 8) { status.textContent = 'Password must be at least 8 characters.'; return; }
    if (pwd !== pwd2) { status.textContent = 'Passwords don’t match.'; return; }
    const btn = document.getElementById('pwd-save');
    btn.disabled = true;
    status.textContent = '';
    try {
      await api.changePassword(pwd);
      await api.logout().catch(() => {});
      location.href = '/login';
    } catch (err) {
      status.textContent = err.message;
      btn.disabled = false;
    }
  };
}
