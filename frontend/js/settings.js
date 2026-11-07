// Settings pages: /settings?s=domain|system|users|backups|notifications|tokens|transfer|logs|account
//
// Each section is a full page (title, description, grouped forms) instead of
// the modals these used to be. The sidebar links here and marks the active one.

import { api, wsSystemLogs } from './api.js';
import { createLogViewer, fillComponentSelect } from './logviewer.js';
import { setCrumbs } from './shell.js';
import { toast, confirm, timeAgo, spinner } from './utils.js';
import { openPanelDomainWizard, openBaseDomainWizard } from './domain-wizard.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

export const SETTINGS_SECTIONS = {
  domain:   { title: 'Domain & SSL',     sub: 'Where the panel and your apps are reachable. HTTPS certificates are requested and renewed automatically.' },
  system:   { title: 'System settings',  sub: 'Session length, port ranges and resource limits for this Cloudbase installation.' },
  users:    { title: 'Users & roles',    sub: 'Who can sign in to this panel, and what each role is allowed to do.' },
  backups:  { title: 'Backup storage',   sub: 'Keep a copy of every database backup outside this server, in S3-compatible storage.' },
  notifications: { title: 'Notifications', sub: 'Where Cloudbase reports problems and deploys — Slack, Teams, PagerDuty, e-mail, webhooks and more — and which events go where.' },
  tokens:   { title: 'Git tokens',       sub: 'Saved access tokens for private repositories on GitHub, GitLab, Bitbucket, Gitea and other Git hosts (HTTPS). Pick one when you create or edit an app.' },
  transfer: { title: 'Export / import',  sub: 'Move app configurations between Cloudbase installations, or keep a backup.' },
  logs:     { title: 'System logs',      sub: 'Everything Cloudbase itself logs — deploys, autoscaler, nginx, nodes, databases — in one place, live.' },
  account:  { title: 'Account',          sub: 'Your own sign-in details.' },
};

const RENDER = {
  domain: renderDomain,
  system: renderSystem,
  logs: renderLogs,
  users: renderUsers,
  backups: renderBackupStorage,
  notifications: renderNotifications,
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

/* ─── Domain & SSL ──────────────────────────────────────────────────────── */

const LE_LIVE = /^\/etc\/letsencrypt\/live\/([a-z0-9.-]+)\/fullchain\.pem$/;
const fmtDay = iso => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });

function renderDomain(root) {
  root.innerHTML = `
    ${group('Cloudbase panel',
      'The address of this dashboard. nginx listens on 80/443 and forwards to Cloudbase.',
      `<div class="https-card" id="panel-card"><span class="settings-note">Loading…</span></div>`)}
    ${group('App subdomains',
      'Give every app an automatic address like <code>myapp.apps.example.com</code>, before or instead of its own domain.',
      `<div class="https-card" id="base-card"><span class="settings-note">Loading…</span></div>`)}
  `;

  const live = { domain: null, cert: null, base: null, baseCert: null };

  const load = () => api.getPDManagerNginx().then(data => {
    const pick = re => ((data.content || '').match(re)?.[1] || '').replace(/^["']|["']$/g, '') || null;
    live.domain = data.exists ? pick(/server_name\s+([^\s;]+)/) : null;
    live.cert = data.exists ? pick(/ssl_certificate\s+([^\s;]+)/) : null;
    live.base = data.base_domain || null;
    live.baseCert = data.base_ssl_cert_path || null;
    renderPanel();
    renderBase();
  }).catch(e => {
    document.getElementById('panel-card').innerHTML = `<span class="settings-note">${esc(e.message)}</span>`;
  });
  load();

  const card = (id, { tone = '', title, text, actions = '' }) => {
    document.getElementById(id).innerHTML = `
      <span class="https-dot ${tone}"></span>
      <span class="https-text"><strong>${title}</strong><span>${text}</span></span>
      <span class="https-actions">${actions}</span>`;
  };

  /* ── Panel ─────────────────────────────────────────────────────────── */
  async function renderPanel() {
    const btn = (id, label, primary = false) => `<button type="button" class="btn btn-sm ${primary ? 'btn-primary' : ''}" id="${id}">${label}</button>`;
    if (!live.domain) {
      card('panel-card', {
        title: 'No domain yet',
        text: `The panel is reachable at <code>${esc(location.host)}</code>. Connect a domain to serve it over HTTPS.`,
        actions: btn('panel-connect', 'Connect a domain', true),
      });
    } else {
      const leName = LE_LIVE.exec(live.cert || '')?.[1];
      let tone = '', text = 'Served over plain HTTP.', actions = btn('panel-secure', 'Set up HTTPS', true);
      if (leName) {
        card('panel-card', { title: esc(live.domain), text: 'Checking certificate…' });
        let info = null;
        try { info = await api.certificateInfo(leName); } catch { /* fall through */ }
        tone = !info?.found ? 'ok' : info.days_left < 0 ? 'bad' : info.days_left < 14 ? 'warn' : 'ok';
        text = !info?.found ? 'HTTPS · free certificate, renews automatically'
          : info.days_left < 0 ? `Certificate expired on ${fmtDay(info.expires_at)} — renewal failed. Check the DNS record and port 80.`
          : info.days_left < 14 ? `Certificate expires in ${info.days_left} days (${fmtDay(info.expires_at)}) — automatic renewal seems to be failing.`
          : `HTTPS · certificate valid until ${fmtDay(info.expires_at)}, renews automatically`;
        actions = '';
      } else if (live.cert) {
        tone = 'warn';
        text = 'HTTPS with a certificate set up earlier, which Cloudbase doesn’t renew.';
        actions = btn('panel-secure', 'Switch to a free certificate', true);
      }
      card('panel-card', { tone, title: esc(live.domain), text, actions: actions + btn('panel-connect', 'Change domain') });
    }
    document.getElementById('panel-connect')?.addEventListener('click', () =>
      openPanelDomainWizard({ cert: live.cert, onDone: load }));
    document.getElementById('panel-secure')?.addEventListener('click', () =>
      openPanelDomainWizard({ domain: live.domain, cert: live.cert, onDone: load }));
  }

  /* ── Base domain ───────────────────────────────────────────────────── */
  function renderBase() {
    const btn = (id, label, primary = false) => `<button type="button" class="btn btn-sm ${primary ? 'btn-primary' : ''}" id="${id}">${label}</button>`;
    if (!live.base) {
      card('base-card', {
        title: 'Off',
        text: 'Apps are only reachable on the domains you connect to them.',
        actions: btn('base-setup', 'Set up app subdomains', true),
      });
    } else if (live.baseCert) {
      card('base-card', {
        tone: 'warn',
        title: `*.${esc(live.base)}`,
        text: 'Uses a wildcard certificate set up earlier, which Cloudbase doesn’t renew. Switch to automatic: every app gets its own certificate that renews itself.',
        actions: btn('base-auto', 'Switch to automatic', true) + btn('base-setup', 'Change') + btn('base-off', 'Turn off'),
      });
    } else {
      card('base-card', {
        tone: 'ok',
        title: `*.${esc(live.base)}`,
        text: 'Apps are reachable at <code>&lt;app-name&gt;.' + esc(live.base) + '</code>. HTTPS is set up automatically per app — the status is under each app’s Settings → Domains.',
        actions: btn('base-setup', 'Change') + btn('base-off', 'Turn off'),
      });
    }
    document.getElementById('base-setup')?.addEventListener('click', () =>
      openBaseDomainWizard({ onDone: load }));
    document.getElementById('base-auto')?.addEventListener('click', async () => {
      try { await api.setBaseDomain(live.base); toast('App subdomains now get automatic certificates'); load(); }
      catch (e) { toast(e.message, 'error'); }
    });
    document.getElementById('base-off')?.addEventListener('click', async () => {
      if (!await confirm('Turn off app subdomains?', `Apps stop answering on &lt;app-name&gt;.${esc(live.base)}. Domains you connected to apps keep working.`)) return;
      try { await api.setBaseDomain(null); toast('App subdomains turned off'); load(); }
      catch (e) { toast(e.message, 'error'); }
    });
  }
}

/* ─── System settings ───────────────────────────────────────────────────── */

const SYS_FIELDS = [
  ['auth', 'token_expire_seconds', 'sys-token-expire'],
  ['ports', 'instance_min', 'sys-inst-min'], ['ports', 'instance_max', 'sys-inst-max'],
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
    ${group('Node network', 'Instances on other nodes are reached through a private WireGuard network between the primary and each node. Nothing else is exposed.',
      `<div id="wg-status" class="field-hint">Checking…</div>
      <div class="field">
        <label class="field-label" for="sys-wg-endpoint">Address of this primary</label>
        <input class="input" id="sys-wg-endpoint" type="text" placeholder="found automatically" />
        <div class="field-hint">Only needed if the automatically found public IP is wrong. Nodes reach this primary on UDP port <code id="wg-port">51820</code>, which must be forwarded to this server. A panel domain behind Cloudflare’s proxy is not used for this.</div>
      </div>`)}
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
    document.getElementById('sys-wg-endpoint').value = data.wireguard?.endpoint ?? '';
  }).catch(e => setStatus(e.message, 'error'));

  api.getMesh().then(m => {
    document.getElementById('wg-port').textContent = m.port;
    const el = document.getElementById('wg-status');
    el.innerHTML = !m.enabled ? 'The node network is turned off (<code>wireguard.enabled</code> in config.yaml).'
      : m.up ? `Running on <code>${esc(m.subnet)}</code>, UDP port <code>${esc(m.port)}</code>.`
      : m.error ? `Not running: ${esc(m.error)} — run <code>cloudbase wireguard</code> on this server.`
      : 'Starts when the first node joins.';
  }).catch(() => {});

  const save = document.getElementById('system-save');
  save.onclick = async () => {
    const body = { auth: {}, ports: {}, limits: {}, wireguard: { endpoint: document.getElementById('sys-wg-endpoint').value.trim() } };
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

/* ─── System logs ───────────────────────────────────────────────────────── */

function fmtBytes(n) {
  if (!n) return 'empty';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function renderLogs(root) {
  const params = new URLSearchParams(location.search);
  let source = params.get('source') || 'server';

  root.innerHTML = `
    <div class="syslog-sources" id="syslog-sources" role="tablist" aria-label="Log source"></div>
    <p class="syslog-source-desc" id="syslog-source-desc"></p>
    <div class="logs-panel syslog-panel">
      <div class="logs-toolbar">
        <select class="input" id="syslog-component" aria-label="Component"><option value="">All components</option></select>
        <select class="input" id="syslog-level" aria-label="Level">
          <option value="">All levels</option>
          <option value="WARNING">Warnings &amp; errors</option>
          <option value="ERROR">Errors only</option>
        </select>
        <input class="input syslog-search" id="syslog-q" type="search" placeholder="Search…" spellcheck="false" />
        <span class="logs-live"><span class="dot"></span><span>Live</span></span>
      </div>
      <div class="log-terminal" id="syslog-out"><div class="log-empty">Loading…</div></div>
    </div>`;

  const compSel = document.getElementById('syslog-component');
  const levelSel = document.getElementById('syslog-level');
  const qInput = document.getElementById('syslog-q');

  const viewer = createLogViewer(document.getElementById('syslog-out'), {
    load: f => api.getSystemLogs({ source, limit: 1000, ...f }),
    stream: onEntries => wsSystemLogs(source, onEntries),
    onComponents: names => fillComponentSelect(compSel, names, viewer.filters.component),
  });
  viewer.filters.component = params.get('component') || '';

  function syncUrl() {
    const p = new URLSearchParams(location.search);
    p.set('source', source);
    if (viewer.filters.component) p.set('component', viewer.filters.component); else p.delete('component');
    history.replaceState(null, '', `${location.pathname}?${p}`);
  }

  async function loadSources() {
    const box = document.getElementById('syslog-sources');
    const desc = document.getElementById('syslog-source-desc');
    let sources = [];
    try {
      sources = (await api.getLogSources()).sources;
    } catch (e) {
      box.innerHTML = `<div class="settings-notice" data-tone="error">${esc(e.message)}</div>`;
      return;
    }
    if (!sources.some(s => s.key === source)) source = 'server';
    const draw = () => {
      box.innerHTML = sources.map(s => `
        <button type="button" role="tab" class="syslog-source${s.key === source ? ' active' : ''}"
          data-key="${s.key}" aria-selected="${s.key === source}" ${s.readable ? '' : 'disabled'}
          title="${esc(s.path)}">
          <span>${esc(s.label)}</span>
          <span class="syslog-source-size">${s.exists ? (s.readable ? fmtBytes(s.size) : 'no access') : 'missing'}</span>
        </button>`).join('');
      const cur = sources.find(s => s.key === source);
      desc.innerHTML = cur ? `${esc(cur.description)} <code>${esc(cur.path)}</code>` : '';
    };
    draw();
    box.onclick = ev => {
      const btn = ev.target.closest('.syslog-source');
      if (!btn || btn.disabled || btn.dataset.key === source) return;
      source = btn.dataset.key;
      viewer.filters.component = '';
      draw();
      syncUrl();
      viewer.start();
    };
  }

  compSel.onchange = () => { viewer.setFilter('component', compSel.value); syncUrl(); };
  levelSel.onchange = () => viewer.setFilter('level', levelSel.value);
  let qTimer;
  qInput.oninput = () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => viewer.setFilter('q', qInput.value.trim()), 300);
  };
  window.addEventListener('pagehide', () => viewer.stop(), { once: true });

  loadSources().then(() => { syncUrl(); viewer.start(); });
}

/* ─── Backup storage ────────────────────────────────────────────────────── */

function textField(id, label, hint, attrs = '') {
  return `
    <div class="field">
      <label class="field-label" for="${id}">${label}</label>
      <input class="input" id="${id}" ${attrs} />
      ${hint ? `<div class="field-hint">${hint}</div>` : ''}
    </div>`;
}

function renderBackupStorage(root) {
  root.innerHTML = `
    ${actionBar('Database backups are always kept on the primary; this adds a copy elsewhere.', 'storage-save', 'Save')}
    ${group('Off-site copies', 'When on, every new backup is uploaded right after it is made. Existing backups are uploaded too, and failed uploads are retried every hour.',
      `<label class="toggle-row form-toggle">
        <span><span class="field-label">Copy backups to S3-compatible storage</span><span class="field-hint" id="st-counts"></span></span>
        <span class="toggle"><input type="checkbox" id="st-enabled" /><span class="toggle-slider"></span></span>
      </label>`)}
    ${group('Bucket', 'AWS S3, Cloudflare R2, Backblaze B2, MinIO or any other S3-compatible service.',
      `${textField('st-endpoint', 'Endpoint', 'Empty for AWS S3. R2: <code>https://&lt;account-id&gt;.r2.cloudflarestorage.com</code> · Backblaze: <code>https://s3.&lt;region&gt;.backblazeb2.com</code>', 'placeholder="https://…" spellcheck="false"')}
      <div class="form-pair">
        ${textField('st-bucket', 'Bucket', '', 'spellcheck="false"')}
        ${textField('st-region', 'Region', 'e.g. <code>eu-central-1</code>; <code>auto</code> for R2', 'spellcheck="false"')}
      </div>
      ${textField('st-prefix', 'Folder in the bucket', 'Backups go to <code>&lt;folder&gt;database-backups/</code>. Use one folder per Cloudbase server.', 'spellcheck="false"')}`)}
    ${group('Access key', 'Give this key access to the bucket only. The secret is stored encrypted and never shown again.',
      `<div class="form-pair">
        ${textField('st-access', 'Access key ID', '', 'spellcheck="false" autocomplete="off"')}
        ${textField('st-secret', 'Secret access key', '', 'type="password" autocomplete="new-password"')}
      </div>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-sm" id="storage-test">Test connection</button>
        <span class="field-hint" id="storage-test-result" style="margin:0"></span>
      </div>`)}
    ${group('Encryption', 'Encrypt each backup on this server before it is uploaded (AES-256-GCM), so the bucket only ever holds unreadable files. Copies on this server stay as they are.',
      `<label class="toggle-row form-toggle">
        <span><span class="field-label">Encrypt backups before upload</span><span class="field-hint" id="st-key-state"></span></span>
        <span class="toggle"><input type="checkbox" id="st-encrypt" /><span class="toggle-slider"></span></span>
      </label>
      <div class="field-hint" id="st-key-warning" style="color:var(--yellow)">Store the key somewhere outside this server (a password manager). Without it, encrypted backups in the bucket cannot be read, for example after this server's disk fails.</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
        <button class="btn btn-sm" id="st-reveal">Show key</button>
      </div>
      <div class="field" style="margin:16px 0 0">
        <label class="field-label" for="st-add-key">Add a key from another server</label>
        <div style="display:flex;gap:8px">
          <input class="input input-mono" id="st-add-key" placeholder="cbk1_…" spellcheck="false" autocomplete="off" />
          <button class="btn" id="st-add-key-btn">Add</button>
        </div>
        <div class="field-hint">Needed to restore encrypted backups that another (or an earlier) Cloudbase server uploaded.</div>
      </div>`)}
  `;

  const $ = id => document.getElementById(id);
  const form = () => ({
    enabled: $('st-enabled').checked,
    endpoint: $('st-endpoint').value.trim(),
    region: $('st-region').value.trim(),
    bucket: $('st-bucket').value.trim(),
    prefix: $('st-prefix').value.trim(),
    access_key: $('st-access').value.trim(),
    secret_key: $('st-secret').value,
    encrypt: $('st-encrypt').checked,
  });
  const show = data => {
    $('st-enabled').checked = !!data.enabled;
    $('st-endpoint').value = data.endpoint || '';
    $('st-region').value = data.region || '';
    $('st-bucket').value = data.bucket || '';
    $('st-prefix').value = data.prefix || 'cloudbase/';
    $('st-access').value = data.access_key || '';
    $('st-secret').value = '';
    $('st-secret').placeholder = data.secret_set ? 'Saved; leave empty to keep' : '';
    $('st-encrypt').checked = !!data.encrypt;
    $('st-reveal').disabled = !data.current_key_id;
    $('st-key-state').textContent = data.current_key_id
      ? `Current key ${data.current_key_id}` + (data.key_ids.length > 1 ? ` · ${data.key_ids.length} keys known` : '')
      : 'A key is created when you turn this on and save';
    const c = data.counts;
    $('st-counts').textContent = c
      ? `${c.uploaded} backup${c.uploaded === 1 ? '' : 's'} copied` + (c.pending ? ` · ${c.pending} waiting` : '') + (c.failed ? ` · ${c.failed} failed` : '')
      : '';
  };

  api.getBackupStorage().then(show).catch(e => setStatus(e.message, 'error'));

  $('st-reveal').onclick = async () => {
    let res;
    try { res = await api.revealBackupKey(); } catch (e) { setStatus(e.message, 'error'); return; }
    const backdrop = document.createElement('div');
    backdrop.className = 'dialog-backdrop';
    backdrop.innerHTML = `
      <div class="dialog dialog-modern form-dialog" role="dialog" aria-modal="true" aria-labelledby="key-title">
        <div class="dialog-title" id="key-title">Backup encryption key</div>
        <div class="dialog-body">
          <p class="form-dialog-intro">Save this key in a password manager or another safe place outside this server. Anyone with the key and your bucket can read your backups.</p>
          <code class="key-box" id="key-value">${esc(res.key)}</code>
        </div>
        <div class="dialog-actions">
          <button class="btn" id="key-copy">Copy</button>
          <button class="btn btn-primary" id="key-close">I saved it</button>
        </div>
      </div>`;
    document.body.appendChild(backdrop);
    backdrop.querySelector('#key-copy').onclick = async () => {
      await navigator.clipboard.writeText(res.key);
      toast('Key copied');
    };
    backdrop.querySelector('#key-close').onclick = () => backdrop.remove();
  };

  $('st-add-key-btn').onclick = async () => {
    const key = $('st-add-key').value.trim();
    if (!key) return;
    try {
      show({ ...(await api.addBackupKey(key)), counts: null });
      $('st-add-key').value = '';
      toast('Key added');
      api.getBackupStorage().then(show).catch(() => {});
    } catch (e) { toast(e.message, 'error'); }
  };

  $('storage-test').onclick = async () => {
    const btn = $('storage-test');
    const out = $('storage-test-result');
    btn.disabled = true;
    out.style.color = '';
    out.textContent = 'Testing…';
    try {
      const res = await api.testBackupStorage(form());
      out.style.color = res.ok ? 'var(--green)' : 'var(--red)';
      out.textContent = res.ok ? 'Connected: wrote, read and removed a test file.' : res.error;
    } catch (e) {
      out.style.color = 'var(--red)';
      out.textContent = e.message;
    } finally {
      btn.disabled = false;
    }
  };

  $('storage-save').onclick = async () => {
    const save = $('storage-save');
    save.disabled = true;
    setStatus('Saving…');
    try {
      show(await api.saveBackupStorage(form()));
      setStatus('Saved', 'ok');
      setTimeout(() => api.getBackupStorage().then(show).catch(() => {}), 3000);
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

const PERM_GROUP_LABELS = { apps: 'Apps', nodes: 'Nodes', system: 'System', users: 'Users', roles: 'Roles', audit: 'Audit log', tokens: 'Git tokens', secrets: 'Secrets' };

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

/* ─── Git tokens ─────────────────────────────────────────────────────── */

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

/* ─── Notifications ─────────────────────────────────────────────────────── */

function renderNotifications(root) {
  root.innerHTML = `
    ${actionBar('Cloudbase sends a problem once, and the all-clear when it is fixed.', 'notif-save', 'Save changes')}
    ${group('Channels', 'Each channel stores its tokens and webhook URLs encrypted. A channel can be limited to certain apps, for example a team’s own Slack channel.',
      `<div id="nc-list"><div class="settings-note">Loading…</div></div>
       <div class="field" style="margin:12px 0 0"><button class="btn btn-sm" id="nc-add">Add channel</button></div>`)}
    ${group('Events', 'What goes where. <strong>In-app</strong> is the bell in the top bar; turn an event off there to keep the inbox quiet. A problem is sent once, and its all-clear goes to the same places when it’s fixed.',
      `<div id="nc-matrix"><div class="settings-note">Loading…</div></div>`, 'settings-group--matrix')}
    ${group('Rules', 'When something counts as a problem, whether open problems are repeated, and whether the all-clear is sent.',
      `<div id="nc-rules"><div class="settings-note">Loading…</div></div>`)}
    ${group('Delivery log', 'The last messages that were sent, or that are being retried. Failed messages are retried after 30 seconds, 2 and 10 minutes, half an hour and an hour.',
      `<div id="nc-log" class="scroll-list"><div class="settings-note">Loading…</div></div>`)}
  `;

  const state = { catalog: null, settings: null, channels: [], apps: [], dirty: new Set() };
  const typeLabel = t => state.catalog?.channel_types.find(c => c.type === t)?.label || t;
  const saveBtn = document.getElementById('notif-save');
  saveBtn.disabled = true;
  const markDirty = what => {
    state.dirty.add(what);
    saveBtn.disabled = false;
    setStatus('Unsaved changes');
  };

  const load = async () => {
    try {
      const [catalog, settings, channels, apps] = await Promise.all([
        state.catalog || api.getNotificationCatalog(),
        api.getNotificationSettings(),
        api.listNotificationChannels(),
        api.listApps().catch(() => []),
      ]);
      Object.assign(state, { catalog, settings, channels, apps });
      state.dirty.clear();
      saveBtn.disabled = true;
      setStatus('');
      renderChannelList(state, typeLabel, load);
      renderMatrix(state);
      renderRules(state);
    } catch (e) {
      document.getElementById('nc-list').innerHTML = notice(esc(e.message));
    }
    loadDeliveryLog();
  };
  load();

  document.getElementById('nc-add').onclick = () => state.catalog && openChannelDialog(state, null, load);

  document.getElementById('nc-matrix').addEventListener('change', e => {
    const box = e.target.closest('input[data-ch]');
    if (!box) return;
    const order = state.catalog.events.map(ev => ev.key);
    const toggle = list => {
      const set = new Set(list);
      box.checked ? set.add(box.dataset.ev) : set.delete(box.dataset.ev);
      return order.filter(k => set.has(k));
    };
    if (box.dataset.ch === 'inbox') {
      state.settings.inbox_events = toggle(state.settings.inbox_events);
      markDirty('settings');
    } else {
      const ch = state.channels.find(c => String(c.id) === box.dataset.ch);
      ch.events = toggle(ch.events);
      markDirty(ch.id);
    }
  });

  const rules = document.getElementById('nc-rules');
  rules.addEventListener('click', e => {
    const btn = e.target.closest('[data-rule] button');
    if (!btn) return;
    const group = btn.closest('[data-rule]');
    group.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    state.settings[group.dataset.rule] = parseInt(btn.dataset.value, 10);
    markDirty('settings');
  });
  rules.addEventListener('change', e => {
    if (e.target.id !== 'nr-send-resolved') return;
    state.settings.send_resolved = e.target.checked;
    markDirty('settings');
    renderMatrix(state);
  });

  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    setStatus('Saving…');
    try {
      for (const what of state.dirty) {
        if (what === 'settings') await api.saveNotificationSettings(state.settings);
        else await api.updateNotificationChannel(what, channelPayload(state.channels.find(c => c.id === what)));
      }
      state.dirty.clear();
      setStatus('Saved', 'ok');
    } catch (e) {
      setStatus(e.message, 'error');
      saveBtn.disabled = false;
    }
  };
}

const RULES = [
  { key: 'node_offline_minutes', title: 'Offline node', desc: 'Report a node after it has been offline this long. Short network blips stay quiet.',
    option: v => `${v} min` },
  { key: 'down_minutes', title: 'Failed instance or database', desc: 'Report after it has been down this long, so a short failure during a deploy isn’t reported.',
    option: v => `${v} min` },
  { key: 'disk_percent', title: 'Disk almost full', desc: 'Warn when a node’s disk is fuller than this. The warning clears 5 points lower.',
    option: v => `${v}%` },
  { key: 'cert_days', title: 'Certificate not renewing', desc: 'Warn this many days before a certificate expires. Let’s Encrypt renews 30 days ahead.',
    option: v => `${v} days` },
  { key: 'remind_hours', title: 'Repeat open problems', desc: 'Send warnings and critical problems that are still open again, to the channels that got them.',
    option: v => v === 0 ? 'Off' : `${v}h` },
];

function renderRules(state) {
  const el = document.getElementById('nc-rules');
  el.innerHTML = `
    <div class="s3-card nc-rules">
      ${RULES.map(r => `
        <div class="s3-row">
          <span class="s3-label"><span class="s3-title">${r.title}</span><span class="s3-desc">${r.desc}</span></span>
          <div class="segmented" role="group" aria-label="${r.title}" data-rule="${r.key}">
            ${state.catalog.setting_choices[r.key].map(v =>
              `<button type="button" data-value="${v}" aria-pressed="${state.settings[r.key] === v}">${r.option(v)}</button>`).join('')}
          </div>
        </div>`).join('')}
      <label class="s3-row">
        <span class="s3-label"><span class="s3-title">Send the all-clear</span>
          <span class="s3-desc">When a problem is fixed — a node is back online, a backup works again — say so, in the inbox and to every channel that got the alert. PagerDuty then closes its incident.</span></span>
        <span class="toggle"><input type="checkbox" id="nr-send-resolved" ${state.settings.send_resolved ? 'checked' : ''} /><span class="toggle-slider"></span></span>
      </label>
    </div>`;
}

/** A channel as it is sent back on save: empty secrets keep the stored ones. */
function channelPayload(ch, overrides = {}) {
  const { _secrets_set, ...config } = ch.config || {};
  return { name: ch.name, type: ch.type, enabled: ch.enabled, events: ch.events, app_ids: ch.app_ids, config, ...overrides };
}

function renderChannelList(state, typeLabel, reload) {
  const list = document.getElementById('nc-list');
  if (!state.channels.length) {
    list.innerHTML = '<div class="settings-note">No channels yet. Notifications still show up under the bell in the top bar.</div>';
    return;
  }
  const appName = id => state.apps.find(a => a.id === id)?.name || `app ${id}`;
  list.innerHTML = state.channels.map(ch => {
    const tone = !ch.enabled ? '' : ch.last_status === 'failed' ? 'bad' : ch.last_status === 'sent' ? 'ok' : '';
    const scope = ch.app_ids.length ? `Only ${ch.app_ids.map(appName).map(esc).join(', ')}` : 'All apps and the platform';
    const last = !ch.enabled ? 'Turned off'
      : ch.last_status === 'failed' ? `Last message failed: ${esc(ch.last_error || '')}`
      : ch.last_sent_at ? `Last message ${timeAgo(ch.last_sent_at)}` : 'Nothing sent yet';
    return `
      <div class="https-card nc-card" data-id="${ch.id}">
        <span class="https-dot ${tone}"></span>
        <span class="https-text">
          <strong>${esc(ch.name)} <span class="nc-type">${esc(typeLabel(ch.type))}</span></strong>
          <span>${scope} · ${ch.events.length} event${ch.events.length === 1 ? '' : 's'}</span>
          <span class="${ch.last_status === 'failed' && ch.enabled ? 'nc-error' : ''}">${last}</span>
        </span>
        <span class="https-actions">
          <button class="btn btn-sm" data-act="test">Test</button>
          <button class="btn btn-sm" data-act="edit">Edit</button>
          <button class="btn btn-sm btn-ghost btn-danger-text" data-act="delete">Delete</button>
        </span>
      </div>`;
  }).join('');

  list.querySelectorAll('[data-act]').forEach(btn => {
    const ch = state.channels.find(c => String(c.id) === btn.closest('[data-id]').dataset.id);
    btn.onclick = async () => {
      if (btn.dataset.act === 'edit') return openChannelDialog(state, ch, reload);
      if (btn.dataset.act === 'delete') {
        if (!await confirm(`Delete channel "${ch.name}"?`, 'Messages that are still waiting for it are dropped.')) return;
        try { await api.deleteNotificationChannel(ch.id); toast('Channel deleted'); reload(); }
        catch (e) { toast(e.message, 'error'); }
        return;
      }
      btn.disabled = true;
      btn.textContent = 'Sending…';
      try {
        const res = await api.testNotificationChannel({ type: ch.type, config: channelPayload(ch).config, channel_id: ch.id });
        res.ok ? toast(`Test message sent to ${ch.name}`) : toast(res.error, 'error');
      } catch (e) { toast(e.message, 'error'); }
      btn.disabled = false;
      btn.textContent = 'Test';
    };
  });
}

function renderMatrix(state) {
  const el = document.getElementById('nc-matrix');
  const columns = [
    { id: 'inbox', name: 'In-app', title: 'Under the bell in the top bar', events: state.settings.inbox_events },
    ...state.channels.map(ch => ({ id: ch.id, name: ch.name, title: ch.name, events: ch.events })),
  ];
  const check = (col, ev) => `
    <label class="nc-check">
      <input type="checkbox" data-ch="${col.id}" data-ev="${ev.key}" ${col.events.includes(ev.key) ? 'checked' : ''}
        aria-label="${esc(ev.label)}: ${esc(col.name)}" /><span aria-hidden="true"></span>
    </label>`;
  let lastGroup = null;
  const rows = state.catalog.events.map(ev => {
    const head = ev.group !== lastGroup
      ? `<tr class="nc-group"><td colspan="${columns.length + 1}"><span>${esc(ev.group)}</span></td></tr>` : '';
    lastGroup = ev.group;
    return head + `
      <tr>
        <td class="nc-event"><span class="nc-sev" data-sev="${ev.severity}"></span>${esc(ev.label)}${ev.problem && state.settings.send_resolved
          ? '<span class="nc-event-sub" title="Also sends the all-clear when it’s fixed">+ all-clear</span>' : ''}</td>
        ${columns.map(col => `<td class="nc-cell${col.id === 'inbox' ? ' nc-inbox' : ''}">${check(col, ev)}</td>`).join('')}
      </tr>`;
  }).join('');
  el.innerHTML = `
    <div class="nc-matrix-wrap">
      <table class="table nc-matrix">
        <thead><tr><th></th>${columns.map(col => `
          <th class="nc-cell${col.id === 'inbox' ? ' nc-inbox' : ''}"><span class="nc-ch-name" title="${esc(col.title)}">${esc(col.name)}</span></th>`).join('')}
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${state.channels.length ? '' : '<div class="field-hint" style="margin-top:10px">Add a channel above to also send events to Slack, e-mail, PagerDuty and others.</div>'}`;
}

async function loadDeliveryLog() {
  const el = document.getElementById('nc-log');
  if (!el) return;
  let rows;
  try { rows = await api.listNotificationDeliveries(30); }
  catch (e) { el.innerHTML = notice(esc(e.message)); return; }
  if (!rows.length) {
    el.innerHTML = '<div class="settings-note">Nothing sent yet.</div>';
    return;
  }
  const status = d => d.status === 'sent' ? `<span class="badge badge-running">Sent</span>`
    : d.status === 'failed' ? `<span class="badge badge-error">Failed</span>`
    : `<span class="badge badge-pending">${d.attempts ? `Retrying (${d.attempts})` : 'Sending'}</span>`;
  el.innerHTML = `
    <table class="table settings-table">
      <thead><tr><th>Message</th><th>Channel</th><th>Status</th><th></th></tr></thead>
      <tbody>${rows.map(d => `
        <tr data-id="${d.id}">
          <td><div class="nc-log-title">${esc(d.notification.title)}</div>
              <div class="cell-muted nc-log-sub">${timeAgo(d.created_at)}${d.error ? ` · ${esc(d.error)}` : ''}</div></td>
          <td>${esc(d.channel.name)}</td>
          <td>${status(d)}</td>
          <td class="row-actions">${d.status !== 'sent' ? '<button class="btn btn-sm btn-ghost" data-act="retry">Retry now</button>' : ''}</td>
        </tr>`).join('')}</tbody>
    </table>`;
  el.querySelectorAll('[data-act="retry"]').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await api.retryNotificationDelivery(btn.closest('tr').dataset.id);
        setTimeout(loadDeliveryLog, 2500);
      } catch (e) { toast(e.message, 'error'); btn.disabled = false; }
    };
  });
}

function openChannelDialog(state, ch, reload) {
  const edit = !!ch;
  const types = state.catalog.channel_types;
  const fieldsHTML = (type, values = {}) => {
    const spec = types.find(t => t.type === type);
    const secrets = new Set(values._secrets_set || []);
    return spec.fields.map(f => {
      const id = `ncf-${f.key}`;
      const hint = f.help ? `<div class="field-hint">${esc(f.help)}</div>` : '';
      const label = `<label class="field-label" for="${id}">${esc(f.label)}${f.required ? '' : ' <span class="hint">optional</span>'}</label>`;
      if (f.kind.startsWith('select:')) {
        const opts = f.kind.slice(7).split('|');
        return `<div class="field">${label}<select class="input" id="${id}" data-key="${f.key}">
          ${opts.map(o => `<option value="${o}" ${values[f.key] === o ? 'selected' : ''}>${{ starttls: 'STARTTLS', tls: 'TLS', none: 'None' }[o] || o}</option>`).join('')}
        </select>${hint}</div>`;
      }
      const secret = f.kind === 'secret';
      const placeholder = secret && secrets.has(f.key) ? 'Saved; leave empty to keep' : '';
      const value = secret ? '' : esc(values[f.key] ?? (f.key === 'smtp_port' ? '587' : f.key === 'server' ? 'https://ntfy.sh' : ''));
      return `<div class="field">${label}<input class="input${secret ? ' input-mono' : ''}" id="${id}" data-key="${f.key}"
        ${secret ? 'type="password" autocomplete="new-password"' : f.kind === 'number' ? 'inputmode="numeric"' : ''}
        value="${value}" placeholder="${placeholder}" spellcheck="false" />${hint}</div>`;
    }).join('');
  };

  const dlg = openDialog(edit ? `Edit ${esc(ch.name)}` : 'New notification channel', `
    <div class="form-pair">
      <div class="field">
        <label class="field-label" for="nc-type">Type</label>
        <select class="input" id="nc-type" ${edit ? 'disabled' : ''}>
          ${types.map(t => `<option value="${t.type}" ${edit && ch.type === t.type ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label class="field-label" for="nc-name">Name</label>
        <input class="input" id="nc-name" value="${edit ? esc(ch.name) : ''}" placeholder="#alerts-prod" autocomplete="off" />
      </div>
    </div>
    <div id="nc-fields">${fieldsHTML(edit ? ch.type : types[0].type, edit ? ch.config : {})}</div>
    <details class="settings-advanced" ${edit && ch.app_ids.length ? 'open' : ''}>
      <summary>Only for certain apps</summary>
      <div class="settings-advanced-body">
        <div class="field-hint" style="margin:0 0 8px">Leave empty for every app plus nodes, databases and backups. With apps selected, this channel only gets their deploys, instance problems and certificates.</div>
        <div class="check-list nc-apps">
          ${state.apps.length ? state.apps.map(a => `
            <label class="check-row"><input type="checkbox" value="${a.id}" ${edit && ch.app_ids.includes(a.id) ? 'checked' : ''} />
              <span class="check-row-name">${esc(a.name)}</span></label>`).join('') : '<div class="settings-note" style="padding:10px 14px">No apps yet.</div>'}
        </div>
      </div>
    </details>
    <label class="toggle-row form-toggle" style="margin-top:12px">
      <span><span class="field-label">Send messages</span><span class="field-hint">Turn off to pause this channel without losing its settings.</span></span>
      <span class="toggle"><input type="checkbox" id="nc-enabled" ${!edit || ch.enabled ? 'checked' : ''} /><span class="toggle-slider"></span></span>
    </label>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:4px">
      <button class="btn btn-sm" id="nc-test">Send test message</button>
      <span class="field-hint" id="nc-test-result" style="margin:0"></span>
    </div>`, { saveLabel: edit ? 'Save' : 'Add channel', wide: true });

  const $ = sel => dlg.el.querySelector(sel);
  const type = () => $('#nc-type').value;
  $('#nc-type').onchange = () => { $('#nc-fields').innerHTML = fieldsHTML(type()); };
  setTimeout(() => $('#nc-name').focus(), 30);

  const config = () => Object.fromEntries([...dlg.el.querySelectorAll('#nc-fields [data-key]')].map(i => [i.dataset.key, i.value.trim()]));
  const payload = () => ({
    name: $('#nc-name').value.trim() || types.find(t => t.type === type()).label,
    type: type(),
    config: config(),
    enabled: $('#nc-enabled').checked,
    events: edit ? ch.events : state.catalog.default_events,
    app_ids: [...dlg.el.querySelectorAll('.nc-apps input:checked')].map(i => parseInt(i.value, 10)),
  });

  $('#nc-test').onclick = async () => {
    const out = $('#nc-test-result');
    $('#nc-test').disabled = true;
    out.style.color = '';
    out.textContent = 'Sending…';
    try {
      const res = await api.testNotificationChannel({ type: type(), config: config(), channel_id: edit ? ch.id : null });
      out.style.color = res.ok ? 'var(--green)' : 'var(--red)';
      out.textContent = res.ok ? 'Sent. Check that it arrived.' : res.error;
    } catch (e) {
      out.style.color = 'var(--red)';
      out.textContent = e.message;
    }
    $('#nc-test').disabled = false;
  };

  dlg.saveBtn.onclick = async () => {
    dlg.saveBtn.disabled = true;
    dlg.error('');
    try {
      if (edit) await api.updateNotificationChannel(ch.id, payload());
      else await api.createNotificationChannel(payload());
      toast(edit ? 'Channel saved' : 'Channel added');
      dlg.close();
      reload();
    } catch (e) {
      dlg.error(e.message);
      dlg.saveBtn.disabled = false;
    }
  };
}

/* ─── Export / import ───────────────────────────────────────────────────── */

function renderTransfer(root) {
  const plainHttp = location.protocol !== 'https:';
  const can = p => isRoot() || currentPerms().has(p);
  // Secrets are write-only everywhere else, so taking them out needs its own permission.
  const canSecrets = can('secrets.export');
  const canData = can('databases.backup');
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const show = (el, on) => { el.style.display = on ? '' : 'none'; };

  const option = (id, title, sub, allowed, lockText) => `
    <label class="tx-option${allowed ? '' : ' is-disabled'}" id="${id}-wrap" title="${allowed ? '' : esc(lockText)}">
      <input type="checkbox" id="${id}" ${allowed ? '' : 'disabled'} />
      <span class="tx-option-body"><span class="tx-option-title">${title}</span><span class="tx-option-sub">${sub}</span></span>
      ${allowed ? '' : '<span class="tx-lock">needs permission</span>'}
    </label>`;

  root.innerHTML = `
    ${group('Export', 'Download one file with your apps and databases, to set them up again somewhere else.',
      `<div class="tx">
         <section>
           <div class="tx-sec-hd">
             <span class="tx-sec-title">Apps</span><span class="tx-sec-count" id="exp-app-count"></span>
             <label class="check-inline tx-all"><input type="checkbox" id="exp-all" checked /> All</label>
           </div>
           <div id="exp-list" class="check-list"></div>
         </section>
         <section id="exp-db-section" style="display:none">
           <div class="tx-sec-hd">
             <span class="tx-sec-title">Databases</span><span class="tx-sec-count" id="exp-db-count"></span>
             <label class="check-inline tx-all"><input type="checkbox" id="exp-db-all" checked /> All</label>
           </div>
           <div id="exp-db-list" class="check-list"></div>
         </section>
         <section>
           <div class="tx-sec-hd"><span class="tx-sec-title">Also include</span></div>
           <div class="tx-options">
             ${option('exp-secrets', 'Environment variables &amp; Git tokens', 'The values, as plain text. Without this you get the settings and the names of the variables.',
               canSecrets, 'Only the root account can give the “Secrets → export” permission.')}
             <span id="exp-data-slot">${option('exp-data', 'Database data', 'A dump of each selected database, which must be running. The file can get as large as the databases.',
               canData, 'You need the “Database backups” permission.')}</span>
           </div>
         </section>
         <div>
           <div class="tx-bar">
             <div class="tx-bar-info">
               <div class="tx-summary" id="exp-summary"></div>
               <div class="tx-warn" id="exp-warn" style="display:none">Contains secrets or data in plain text. Don’t send it around: move it straight to where you import it, then delete it.</div>
               ${plainHttp ? '<div class="tx-note">This page is not on https: on a private network that works, but the file travels unencrypted.</div>' : ''}
             </div>
             <div class="tx-bar-actions">
               <input class="input tx-pw" id="exp-pw" type="password" placeholder="Your account password" autocomplete="current-password" style="display:none" />
               <button class="btn btn-primary" id="exp-go">Download export</button>
             </div>
           </div>
           <div class="tx-run" id="exp-run" style="display:none"></div>
         </div>
       </div>`)}
    ${group('Import', 'Set up apps and databases from an export file. Names that already exist are skipped.',
      `<div class="tx">
         <div class="tx-file" id="imp-drop">
           <span class="tx-file-name" id="imp-name">Drop an export file here, or choose one</span>
           <button type="button" class="btn btn-sm" id="imp-browse">Choose file</button>
           <input type="file" id="imp-file" accept=".cloudbase,application/octet-stream" hidden>
         </div>
         <div class="tx-bar">
           <div class="tx-bar-info">
             <div class="tx-inline">
               <label class="field-label" for="imp-node" style="margin:0">Place on</label>
               <select class="input" id="imp-node"><option value="">This server (primary)</option></select>
             </div>
             <div class="tx-note">Instances and databases all go to this node; scale to others afterwards.</div>
           </div>
           <div class="tx-bar-actions"><button class="btn btn-primary" id="imp-go" disabled>Import</button></div>
         </div>
         <div id="imp-result"></div>
       </div>`)}
  `;

  // ── Export: what to export ────────────────────────────────────────────────
  const expList = document.getElementById('exp-list');
  const dbSection = document.getElementById('exp-db-section');
  const dbList = document.getElementById('exp-db-list');
  const expAll = document.getElementById('exp-all');
  const dbAll = document.getElementById('exp-db-all');
  const expSecrets = document.getElementById('exp-secrets');
  const expData = document.getElementById('exp-data');
  const expPw = document.getElementById('exp-pw');
  const summary = document.getElementById('exp-summary');
  const appChecks = () => [...expList.querySelectorAll('.exp-check')];
  const dbChecks = () => [...dbList.querySelectorAll('.exp-db-check')];
  const ticked = list => list.filter(c => c.checked).map(c => parseInt(c.value, 10));
  const withSecrets = () => expSecrets.checked && canSecrets;
  const withData = () => expData.checked && canData && ticked(dbChecks()).length > 0;

  const sync = () => {
    const apps = ticked(appChecks()).length, dbs = ticked(dbChecks()).length;
    document.getElementById('exp-app-count').textContent = appChecks().length ? `${apps} of ${appChecks().length}` : '';
    document.getElementById('exp-db-count').textContent = dbChecks().length ? `${dbs} of ${dbChecks().length}` : '';
    const parts = [];
    if (apps) parts.push(plural(apps, 'app'));
    if (dbs) parts.push(plural(dbs, 'database'));
    if (withSecrets()) parts.push('with secrets');
    if (withData()) parts.push('with data');
    summary.textContent = parts.length ? parts.join(' · ') : 'Nothing selected';
    show(document.getElementById('exp-warn'), withSecrets() || withData());
    show(expPw, withSecrets());
    // the data option only makes sense when a database is selected
    document.getElementById('exp-data-wrap').style.opacity = dbs || !canData ? '' : '.6';
  };

  api.listApps().then(apps => {
    if (!apps.length) { expList.outerHTML = '<div class="tx-empty" id="exp-list">No apps to export.</div>'; sync(); return; }
    expList.innerHTML = apps.map(a => `
      <label class="check-row">
        <input type="checkbox" class="exp-check" value="${a.id}" checked />
        <span class="check-row-name">${esc(a.name)}</span>
        <span class="check-row-meta">${plural((a.replicas || []).length, 'instance')}</span>
      </label>`).join('');
    appChecks().forEach(c => c.onchange = () => { expAll.checked = appChecks().every(x => x.checked); sync(); });
    sync();
  }).catch(e => { expList.innerHTML = notice(esc(e.message)); });
  api.listDatabases().then(dbs => {
    if (!dbs.length) return;
    show(dbSection, true);
    dbList.innerHTML = dbs.map(d => `
      <label class="check-row">
        <input type="checkbox" class="exp-db-check" value="${d.id}" checked />
        <span class="check-row-name">${esc(d.name)}</span>
        <span class="check-row-meta">${esc(d.engine)} ${esc(d.version || '')} · ${esc(d.status || '')}</span>
      </label>`).join('');
    dbChecks().forEach(c => c.onchange = () => { dbAll.checked = dbChecks().every(x => x.checked); sync(); });
    sync();
  }).catch(() => {});
  expAll.onchange = () => { appChecks().forEach(c => { c.checked = expAll.checked; }); sync(); };
  dbAll.onchange = () => { dbChecks().forEach(c => { c.checked = dbAll.checked; }); sync(); };
  expSecrets.onchange = sync;
  expData.onchange = sync;
  sync();

  // ── Export: start, follow, download ───────────────────────────────────────
  const expGo = document.getElementById('exp-go');
  const run = document.getElementById('exp-run');
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  // The progress card: what is happening now, and the steps with the finished ones ticked off.
  const STAGES = [['collect', 'Settings'], ['dump', 'Database data'], ['pack', 'Package'], ['done', 'Download']];
  const renderRun = (status, includeData) => {
    const stages = STAGES.filter(([key]) => includeData || key !== 'dump');
    const at = Math.max(0, stages.findIndex(([key]) => key === status.stage));
    const done = status.state === 'done', failed = status.state === 'failed';
    show(run, true);
    run.dataset.state = status.state;
    run.innerHTML = `
      <div class="tx-run-head">${done ? '<span class="tx-run-mark">✓</span>' : failed ? '<span class="tx-run-mark">✕</span>' : spinner}<span class="tx-run-title">${esc(status.message || '')}</span></div>
      ${done || failed ? '' : '<div class="tx-track"><div class="tx-track-fill"></div></div>'}
      <ol class="tx-steps">${stages.map(([key, label], i) => `<li class="${done || (!failed && i < at) ? 'is-done' : i === at && !failed ? 'is-active' : ''}">${label}</li>`).join('')}</ol>`;
  };

  expGo.onclick = async () => {
    const appIds = ticked(appChecks()), dbIds = ticked(dbChecks());
    if (!appIds.length && !dbIds.length) return toast('Select at least one app or database', 'error');
    if (withSecrets() && !expPw.value) { expPw.focus(); return toast('Enter your account password', 'error'); }
    expGo.disabled = true;
    expGo.innerHTML = `${spinner} Exporting…`;
    const wantsData = withData();
    renderRun({ state: 'running', stage: 'collect', message: 'Starting…' }, wantsData);
    try {
      const { job_id } = await api.startExport({
        app_ids: appIds.length === appChecks().length ? null : appIds,    // null: every app
        database_ids: dbIds,
        include_data: withData(),
        include_secrets: withSecrets(),
        password: expPw.value,
      });
      let status;
      for (;;) {                                  // a database dump can take minutes: follow the job
        await sleep(700);
        status = await api.exportStatus(job_id);
        renderRun(status, wantsData);
        if (status.state !== 'running') break;
      }
      if (status.state === 'failed') throw new Error(status.message);
      const file = await api.downloadExport(job_id);
      const a = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(file.blob),
        download: file.filename || `cloudbase-export-${new Date().toISOString().slice(0, 10)}.cloudbase`,
      });
      a.click();
      URL.revokeObjectURL(a.href);
      expPw.value = '';
      renderRun({ state: 'done', stage: 'done', message: `Downloaded: ${plural(status.apps, 'app')}, ${plural(status.databases, 'database')}.` }, wantsData);
      toast('Export downloaded');
    } catch (e) {
      renderRun({ state: 'failed', message: e.message }, wantsData);
      toast(e.message, 'error');
    } finally {
      expGo.disabled = false;
      expGo.textContent = 'Download export';
    }
  };

  // ── Import ────────────────────────────────────────────────────────────────
  let file = null;
  const drop = document.getElementById('imp-drop');
  const fileInput = document.getElementById('imp-file');
  const impGo = document.getElementById('imp-go');
  const result = document.getElementById('imp-result');
  const choose = f => {
    file = f || null;
    const name = document.getElementById('imp-name');
    name.textContent = file ? `${file.name} (${(file.size / 1048576).toFixed(file.size > 1048576 ? 1 : 2)} MB)` : 'Drop an export file here, or choose one';
    name.classList.toggle('has-value', !!file);
    impGo.disabled = !file;
  };
  document.getElementById('imp-browse').onclick = () => fileInput.click();
  fileInput.onchange = () => choose(fileInput.files[0]);
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('is-over'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('is-over'); }));
  drop.addEventListener('drop', e => choose(e.dataTransfer.files[0]));
  api.listNodes().then(nodes => {
    const sel = document.getElementById('imp-node');
    nodes.filter(n => !n.is_local).forEach(n => sel.insertAdjacentHTML('beforeend', `<option value="${n.id}">${esc(n.name)}</option>`));
  }).catch(() => {});

  const row = (kind, name, text) => `<li data-kind="${kind}">${name ? `<b>${esc(name)}</b>` : ''}<span>${esc(text)}</span></li>`;
  impGo.onclick = async () => {
    impGo.disabled = true;
    result.innerHTML = `<div class="tx-run" data-state="running"><div class="tx-run-head">${spinner}<span class="tx-run-title">Uploading and importing… a large file takes a while.</span></div><div class="tx-track"><div class="tx-track-fill"></div></div></div>`;
    try {
      const nodeVal = document.getElementById('imp-node').value;
      const res = await api.importTransfer(file, nodeVal ? parseInt(nodeVal, 10) : null);
      const items = [
        ...res.imported.map(a => row('ok', a.name, `app · ${plural(a.instances, 'instance')}${a.will_start ? ' · will start' : ''}`)),
        ...res.databases.map(d => row('ok', d.name, `database · ${d.engine} ${d.version}${d.links ? ` · linked to ${plural(d.links, 'app')}` : ''}${d.restoring ? ' · restoring its data' : ''}`)),
        ...res.skipped.map(s => row('skip', s.name, `app skipped: ${s.reason}`)),
        ...res.skipped_databases.map(s => row('skip', s.name, `database skipped: ${s.reason}`)),
        ...res.warnings.map(w => row('warn', '', w)),
      ];
      const done = res.imported.length + res.databases.length;
      const skipped = res.skipped.length + res.skipped_databases.length;
      result.innerHTML = `
        <div class="tx-result-sum">${done} imported${skipped ? `, ${skipped} skipped` : ''}.${done ? ' Building, starting and restoring continue in the background.' : ''} If the file held secrets, delete it now.</div>
        <ul class="tx-list">${items.join('')}</ul>`;
      if (done) toast('Import started');
    } catch (e) {
      result.innerHTML = `<div class="tx-warn">Import failed: ${esc(e.message)}</div>`;
    } finally {
      impGo.disabled = !file;
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
