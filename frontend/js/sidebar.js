import { api, PermissionError } from './api.js';
import { updateIndex } from './shell.js';
import { toast } from './utils.js';

// Global handler: show a clear toast for any unhandled 403 PermissionError
window.addEventListener('unhandledrejection', e => {
  if (e.reason instanceof PermissionError) {
    e.preventDefault();
    toast(`Geen toegang: ${e.reason.message}`, 'error');
  }
});

function esc(str) {
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export async function initSidebar() {
  loadSidebarTree();
  setInterval(loadSidebarTree, 10000);
  wireNodesButton();
  initSessionTimer();
  wireLogout();
  await initRoleBasedUI();
}

function wireLogout() {
  const btn = document.getElementById('btn-logout');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.addEventListener('click', async () => {
    await api.logout().catch(() => {});
    location.href = '/login';
  });
}

async function loadSidebarTree() {
  // Load nodes and apps independently — a 403 on one must not break the other
  const [nodesResult, appsResult] = await Promise.allSettled([
    api.listNodes(),
    api.listApps(),
  ]);
  const nodes = nodesResult.status === 'fulfilled' ? nodesResult.value : [];
  const apps  = appsResult.status  === 'fulfilled' ? appsResult.value  : [];
  // Feeds the rail's Apps / Nodes menus and the command palette
  updateIndex(apps, nodes);
}

function wireNodesButton() {
  const btn = document.getElementById('btn-nodes');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const section = document.getElementById('nodes-section');
    if (section) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } else {
      // On non-dashboard pages, navigate to the dashboard nodes section
      window.location.href = '/#nodes-section';
    }
  });
}

function wireServiceButton() {
  const btn = document.getElementById('btn-install-service');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    let modal = document.getElementById('service-modal-global');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'service-modal-global';
      modal.className = 'dialog-backdrop';
      modal.innerHTML = `
        <div class="dialog" style="max-width:560px;width:90%">
          <div class="dialog-title">Enable Cloudbase Auto Start</div>
          <div class="dialog-body" style="font-size:13px;line-height:1.6">
            <p style="margin:0 0 10px">Run this command to make Cloudbase start automatically on boot:</p>
            <pre id="service-pre-global" style="background:var(--bg-muted);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;overflow-x:auto;white-space:pre;margin:0 0 12px">Loading…</pre>
            <p style="margin:0;color:var(--text-muted);font-size:12px">Requires <code>sudo</code>. Run once on your Linux server.</p>
          </div>
          <div class="dialog-actions">
            <button class="btn btn-secondary" id="service-copy-global">Copy Commands</button>
            <button class="btn btn-primary" id="service-close-global">Close</button>
          </div>
        </div>`;
      document.body.appendChild(modal);

      modal.querySelector('#service-close-global').onclick = () => { modal.style.display = 'none'; };
      modal.querySelector('#service-copy-global').onclick  = () => {
        navigator.clipboard.writeText(modal.querySelector('#service-pre-global').textContent);
        toast('Copied to clipboard');
      };
      modal.addEventListener('click', e => { if (e.target === modal) modal.style.display = 'none'; });
    }

    modal.style.display = 'flex';
    const pre = modal.querySelector('#service-pre-global');
    pre.textContent = 'Loading…';

    try {
      const data = await api.serviceFile();
      pre.textContent = [
        `# Fastest option`,
        `cloudbase enable`,
        ``,
        `# Manual systemd setup`,
        `sudo tee ${data.path} << 'EOF'`,
        data.content.trim(),
        `EOF`,
        ``,
        `sudo systemctl daemon-reload`,
        `sudo systemctl enable --now cloudbase`,
      ].join('\n');
    } catch (e) {
      pre.textContent = `Error: ${e.message}`;
    }
  });
}

// ── Session timer ─────────────────────────────────────────────────────────────
function fmtSeconds(s) {
  if (s <= 0) return 'Expired';
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${m}:${String(sec).padStart(2, '0')}`;
}

async function initSessionTimer() {
  const bar = document.getElementById('session-timer-bar');
  if (!bar) return;

  const fill  = bar.querySelector('.session-timer-fill');
  const label = bar.querySelector('.session-timer-label');

  let remaining = 3600; // fallback
  try {
    const data = await api.getSession();
    remaining = data.expires_in;
  } catch { return; }

  const total = 3600; // fixed token lifetime — percentage relative to full session

  function tick() {
    if (remaining <= 0) {
      label.textContent = 'Session expired — please log in again';
      fill.style.width  = '0%';
      fill.style.background = 'var(--red)';
      return;
    }

    label.textContent = `Session: ${fmtSeconds(remaining)} remaining`;
    const pct = Math.max(0, (remaining / total) * 100);
    fill.style.width = `${pct}%`;

    if (pct < 15) {
      fill.style.background = 'var(--red)';
    } else if (pct < 35) {
      fill.style.background = 'var(--yellow)';
    } else {
      fill.style.background = 'var(--accent)';
    }

    remaining--;
  }

  tick();
  setInterval(tick, 1000);
}

// Exported so the deploy modal and settings page can call it.
// tokenInput    – the visible password <input> (used for display only when a vault token is chosen)
// tokenIdInput  – a hidden <input> that stores the vault token ID (sent to backend instead of raw value)
export async function pickGitHubToken(tokenInput, tokenIdInput) {
  let tokens = [];
  try { tokens = await api.listGitHubTokens(); } catch { return; }
  if (!tokens.length) { toast('No saved tokens — save one under Settings → GitHub tokens', 'warn'); return; }

  document.querySelectorAll('.gh-token-picker').forEach(p => p.remove());

  const picker = document.createElement('div');
  picker.className = 'gh-token-picker cert-picker';
  picker.style.cssText = `position:absolute;z-index:9999;background:var(--pop);border:1px solid var(--line-strong);
    border-radius:6px;max-height:200px;overflow-y:auto;min-width:260px;
    box-shadow:var(--shadow-lg);font-size:12px;`;

  tokens.forEach(t => {
    const row = document.createElement('div');
    row.style.cssText = 'padding:8px 12px;cursor:pointer;color:var(--text);border-radius:6px;display:flex;justify-content:space-between;gap:12px;';
    row.innerHTML = `<span style="font-weight:500">${esc(t.label)}</span><span style="color:var(--text-2);font-family:var(--font-mono)">••••${esc(t.token_hint)}</span>`;
    row.addEventListener('mouseenter', () => row.style.background = 'var(--hover)');
    row.addEventListener('mouseleave', () => row.style.background = '');
    row.addEventListener('click', () => {
      picker.remove();
      // Store only the vault ID server-side; show a non-editable label in the input
      if (tokenIdInput) tokenIdInput.value = t.id;
      // Show the label as a visual indicator — placeholder style
      tokenInput.value = '';
      tokenInput.placeholder = `🔑 ${t.label} (••••${t.token_hint})`;
      tokenInput.dataset.vaultLabel = t.label;
      // Clear the vault selection when the user starts typing a new token manually
      const clearVault = () => {
        if (tokenIdInput) tokenIdInput.value = '';
        tokenInput.placeholder = tokenInput.dataset.origPlaceholder || '';
        delete tokenInput.dataset.vaultLabel;
        tokenInput.removeEventListener('input', clearVault);
      };
      tokenInput.addEventListener('input', clearVault);
    });
    picker.appendChild(row);
  });

  const rect = tokenInput.getBoundingClientRect();
  picker.style.top  = `${rect.bottom + window.scrollY + 4}px`;
  picker.style.left = `${rect.left + window.scrollX}px`;
  picker.style.width = `${Math.max(rect.width, 260)}px`;
  document.body.appendChild(picker);

  const close = e => { if (!picker.contains(e.target) && e.target !== tokenInput) { picker.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}

// ── Role-based UI ─────────────────────────────────────────────────────────────
// Tracks which data-perm values should be hidden (used for dynamic DOM too)
window._hiddenPerms = new Set();

export function _applyPermVisibility(root = document) {
  if (!window._hiddenPerms.size) return;
  root.querySelectorAll('[data-perm]').forEach(el => {
    if (window._hiddenPerms.has(el.dataset.perm)) el.style.display = 'none';
  });
}
window._applyPermVisibility = _applyPermVisibility;

async function initRoleBasedUI() {
  try {
    const data = await api.checkAuth();
    // Accept both new (is_root) and legacy (is_superadmin) backend response shapes
    const isRoot = !!(data.is_root || data.is_superadmin);
    document.body.dataset.role = data.role;
    document.body.dataset.permissions = JSON.stringify(data.permissions || []);
    window.dispatchEvent(new CustomEvent('cloudbase-role-ready', { detail: { role: data.role, username: data.username, permissions: data.permissions || [] } }));

    const perms = new Set(data.permissions || []);

    if (!isRoot) {
      // Build the set of permissions the user LACKS — used to hide data-perm elements
      const ALL_KNOWN_PERMS = [
        'apps.view','apps.deploy','apps.start','apps.stop','apps.restart','apps.pull','apps.scale','apps.configure','apps.delete',
        'nodes.view','nodes.add','nodes.configure','nodes.delete',
        'system.manage',
        'users.manage','roles.manage',
        'audit.view','tokens.manage',
      ];
      ALL_KNOWN_PERMS.forEach(p => { if (!perms.has(p)) window._hiddenPerms.add(p); });

      // Hide static data-perm elements now
      _applyPermVisibility();

      // Inject a CSS rule so dynamically rendered data-perm elements are also hidden
      if (window._hiddenPerms.size) {
        const selectors = [...window._hiddenPerms]
          .map(p => `[data-perm="${p}"]`)
          .join(',');
        const s = document.createElement('style');
        s.id = 'perm-hide-rules';
        s.textContent = `${selectors}{display:none!important}`;
        document.head.appendChild(s);
      }

      // Legacy: hide [data-admin] for users without system.manage
      if (!perms.has('system.manage')) {
        document.querySelectorAll('[data-admin]').forEach(el => { el.style.display = 'none'; });
        const s = document.createElement('style');
        s.textContent = '[data-admin]{display:none!important}';
        document.head.appendChild(s);
      }
    }

    // Show Manage Users & Roles for Root, or anyone with users.manage / roles.manage
    const canManageUsers = isRoot || perms.has('users.manage') || perms.has('roles.manage');
    if (canManageUsers) {
      const btn = document.getElementById('btn-manage-users');
      if (btn) btn.style.display = '';
    }
  } catch {}
}
