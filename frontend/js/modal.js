import { api } from './api.js';
import { icon, spinner, toast } from './utils.js';
import { pickGitHubToken } from './sidebar.js';

// ── Cert picker helper ────────────────────────────────────────────────────────
let _certCache = null;

async function loadCerts() {
  if (_certCache) return _certCache;
  try {
    _certCache = await api.discoverCerts();
  } catch {
    _certCache = { certs: [], keys: [] };
  }
  return _certCache;
}

function showPicker(inputEl, items, label) {
  document.querySelectorAll('.cert-picker').forEach(p => p.remove());

  if (!items.length) {
    toast(`No ${label} found on this machine`, 'warn');
    return;
  }

  const picker = document.createElement('div');
  picker.className = 'cert-picker';
  picker.style.cssText = `
    position:absolute; z-index:9999; background:#141414; border:1px solid #2e2e2e;
    border-radius:6px; max-height:200px; overflow-y:auto; min-width:320px;
    box-shadow:0 8px 24px rgba(0,0,0,.6); font-size:12px;`;

  items.forEach(path => {
    const row = document.createElement('div');
    row.className = 'cert-picker-row';
    row.textContent = path;
    row.style.cssText = 'padding:8px 12px; cursor:pointer; color:#f0f0f0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;';
    row.addEventListener('mouseenter', () => row.style.background = '#222222');
    row.addEventListener('mouseleave', () => row.style.background = '');
    row.addEventListener('click', () => {
      inputEl.value = path;
      picker.remove();
      inputEl.dispatchEvent(new Event('input'));
    });
    picker.appendChild(row);
  });

  const rect = inputEl.getBoundingClientRect();
  picker.style.top  = `${rect.bottom + window.scrollY + 4}px`;
  picker.style.left = `${rect.left  + window.scrollX}px`;
  picker.style.width = `${rect.width}px`;
  document.body.appendChild(picker);

  const close = e => { if (!picker.contains(e.target) && e.target !== inputEl) { picker.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}

// ── Step definitions ──────────────────────────────────────────────────────────

const STEPS = [
  { id: 'type',    label: 'Type'        },
  { id: 'source',  label: 'Source'      },
  { id: 'process', label: 'Process'     },
  { id: 'env',     label: 'Environment' },
  { id: 'docker',  label: 'Docker'      },
];

// ── Main export ───────────────────────────────────────────────────────────────

export function openDeployModal(onSuccess) {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = modalHTML();
  document.body.appendChild(backdrop);

  const modal = backdrop.querySelector('.modal');
  const form  = modal.querySelector('#deploy-form');
  let envCount = 0;
  let currentStep = 0;

  // ── Close ──────────────────────────────────────────────────────────────────
  const close = () => backdrop.remove();
  backdrop.addEventListener('click', e => { if (e.target === backdrop) close(); });
  modal.querySelector('#modal-close').addEventListener('click', close);
  modal.querySelector('#modal-cancel-footer').addEventListener('click', close);

  // ── Step navigation helpers ────────────────────────────────────────────────
  function showStep(idx) {
    currentStep = idx;
    modal.querySelectorAll('.wizard-step').forEach((el, i) => {
      el.style.display = i === idx ? '' : 'none';
    });
    modal.querySelectorAll('.wizard-crumb').forEach((el, i) => {
      el.classList.toggle('active',    i === idx);
      el.classList.toggle('completed', i < idx);
    });
    modal.querySelector('#btn-back').style.display  = idx === 0 ? 'none' : '';
    modal.querySelector('#btn-next').style.display  = idx < STEPS.length - 1 ? '' : 'none';
    modal.querySelector('#modal-submit').style.display = idx === STEPS.length - 1 ? '' : 'none';
    modal.querySelector('#modal-error').style.display = 'none';

    // Hide port field on Process step when Background Worker is selected
    if (idx === 2) {
      const isWorker = modal.querySelector('.app-type-btn.active')?.dataset.type === 'worker';
      modal.querySelector('#f-port-field').style.display = isWorker ? 'none' : '';
    }
  }

  function validateStep(idx) {
    const errEl = modal.querySelector('#modal-error');
    if (idx === 1) {
      const name = modal.querySelector('#f-name').value.trim();
      const repo = modal.querySelector('#f-repo').value.trim();
      if (!name) {
        errEl.textContent = 'Application name is required.';
        errEl.style.display = 'block';
        return false;
      }
      if (!repo) {
        errEl.textContent = 'GitHub repository URL is required.';
        errEl.style.display = 'block';
        return false;
      }
    }
    return true;
  }

  // ── App type buttons ───────────────────────────────────────────────────────
  modal.querySelectorAll('.app-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      modal.querySelectorAll('.app-type-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  // ── Next / Back ────────────────────────────────────────────────────────────
  modal.querySelector('#btn-next').addEventListener('click', () => {
    if (!validateStep(currentStep)) return;
    showStep(currentStep + 1);
  });

  modal.querySelector('#btn-back').addEventListener('click', () => {
    showStep(currentStep - 1);
  });

  // ── Add env var row ────────────────────────────────────────────────────────
  modal.querySelector('#add-env').addEventListener('click', () => addEnvRow(modal, ++envCount));

  // ── Saved GitHub token picker ──────────────────────────────────────────────
  modal.querySelector('#f-token-pick').addEventListener('click', () => {
    pickGitHubToken(modal.querySelector('#f-token'), modal.querySelector('#f-token-id'));
  });

  // ── Submit ─────────────────────────────────────────────────────────────────
  form.addEventListener('submit', async e => {
    e.preventDefault();
    await handleDeploy(modal, form, onSuccess, close);
  });

  showStep(0);
}

// ── HTML ──────────────────────────────────────────────────────────────────────

function modalHTML() {
  const crumbs = STEPS.map((s, i) => `
    <div class="wizard-crumb" data-step="${i}">
      <div class="wizard-crumb-dot">${i + 1}</div>
      <div class="wizard-crumb-label">${s.label}</div>
    </div>
  `).join('<div class="wizard-crumb-line"></div>');

  return `
    <div class="modal deploy-modal">
      <div class="modal-header">
        <div>
          <div class="modal-title">Deploy Application</div>
          <div class="modal-sub">Launch from GitHub with safe defaults</div>
        </div>
        <button class="modal-close" id="modal-close">${icon.x}</button>
      </div>

      <div class="wizard-nav">${crumbs}</div>

      <form id="deploy-form" novalidate>
        <div class="modal-body">

          <!-- Step 1: Type -->
          <div class="wizard-step">
            <div class="wizard-step-title">What kind of app are you deploying?</div>
            <div class="app-type-selector">
              <button type="button" class="app-type-btn active" data-type="web">
                <div class="app-type-icon">${icon.globe}</div>
                <div class="app-type-info">
                  <div class="app-type-name">Web Service</div>
                  <div class="app-type-desc">Serves HTTP traffic with a public URL, port assignment and nginx routing</div>
                </div>
              </button>
              <button type="button" class="app-type-btn" data-type="worker">
                <div class="app-type-icon">${icon.cpu}</div>
                <div class="app-type-info">
                  <div class="app-type-name">Background Worker</div>
                  <div class="app-type-desc">No web server — runs without a port or nginx (Discord bots, queues, cron jobs)</div>
                </div>
              </button>
            </div>
          </div>

          <!-- Step 2: Source -->
          <div class="wizard-step">
            <div class="wizard-step-title">${icon.github} Source</div>
            <div class="deploy-grid deploy-grid--basic">
              <div class="field deploy-field-span-2">
                <label class="field-label">Application Name <span class="req">*</span></label>
                <input class="input" id="f-name" placeholder="my-app" required autocomplete="off" />
              </div>
              <div class="field deploy-field-span-2">
                <label class="field-label">GitHub Repository URL <span class="req">*</span></label>
                <div class="input-icon-wrap">
                  <span class="icon">${icon.github}</span>
                  <input class="input" id="f-repo" placeholder="https://github.com/user/repo" required />
                </div>
              </div>
              <div class="field deploy-field-span-2 deploy-token-field">
                <label class="field-label">GitHub Token <span class="hint">optional for private repositories</span></label>
                <div class="deploy-token-row">
                  <div class="input-icon-wrap deploy-input-grow">
                    <span class="icon">${icon.lock}</span>
                    <input class="input input-mono" id="f-token" type="password" placeholder="ghp_..." autocomplete="current-password" />
                  </div>
                  <button type="button" class="btn btn-secondary btn-sm deploy-token-btn" id="f-token-pick">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z"/></svg>
                    Saved
                  </button>
                </div>
                <input type="hidden" id="f-token-id" />
              </div>
            </div>
          </div>

          <!-- Step 3: Process -->
          <div class="wizard-step">
            <div class="wizard-step-title">${icon.settings} Process</div>
            <div class="deploy-grid">
              <div class="field deploy-field-span-2">
                <label class="field-label">Start Command <span class="hint">auto-detected if empty</span></label>
                <input class="input input-mono" id="f-cmd" placeholder="npm start" />
              </div>
              <div class="field deploy-field-span-2" id="f-port-field">
                <label class="field-label">Internal Port</label>
                <input class="input" id="f-port" type="number" placeholder="3000" style="max-width:160px" />
                <div class="field-hint">The port your app listens on inside the container. Leave empty to auto-detect.</div>
              </div>
            </div>
          </div>

          <!-- Step 4: Environment -->
          <div class="wizard-step">
            <div class="wizard-step-title">${icon.lock} Environment Variables</div>
            <div class="wizard-step-sub">Store secure variables before first boot. You can add more later.</div>
            <div id="env-rows"></div>
            <button type="button" class="add-env-btn" id="add-env">${icon.plus} Add variable</button>
          </div>

          <!-- Step 5: Docker Runtime -->
          <div class="wizard-step">
            <div class="wizard-step-title">${icon.server} Docker Runtime</div>
            <div class="wizard-step-sub">Optional resource limits and filesystem settings. Leave empty for defaults.</div>
            <div class="deploy-grid">
              <div class="field">
                <label class="field-label">CPU Limit</label>
                <input class="input" id="f-docker-cpu" type="number" min="0.1" step="0.1" placeholder="1.0" />
                <div class="field-hint">Cores (e.g. 0.5, 2.0)</div>
              </div>
              <div class="field">
                <label class="field-label">Memory Limit (MB)</label>
                <input class="input" id="f-docker-memory" type="number" min="1" step="1" placeholder="512" />
                <div class="field-hint">Megabytes (e.g. 512, 1024)</div>
              </div>
              <div class="field deploy-field-span-2 deploy-toggle-stack" style="margin-bottom:0">
                <div class="deploy-toggle-item">
                  <div class="toggle-row">
                    <span class="field-label" style="margin-bottom:0">Read-only root filesystem</span>
                    <label class="toggle" for="f-docker-readonly">
                      <input type="checkbox" id="f-docker-readonly" />
                      <span class="toggle-slider"></span>
                    </label>
                  </div>
                </div>
                <div class="deploy-toggle-item">
                  <div class="toggle-row">
                    <span class="field-label" style="margin-bottom:0">Tmpfs at /tmp</span>
                    <label class="toggle" for="f-docker-tmpfs-enabled">
                      <input type="checkbox" id="f-docker-tmpfs-enabled" />
                      <span class="toggle-slider"></span>
                    </label>
                  </div>
                  <div class="deploy-inline-hint-row">
                    <input class="input deploy-tmpfs-input" id="f-docker-tmpfs-size" type="number" min="1" step="1" placeholder="64" />
                    <span class="deploy-inline-hint">MB for /tmp tmpfs</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div id="modal-error" class="modal-error" style="display:none"></div>
        </div>

        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" id="btn-back" style="display:none">${icon.chevronL} Back</button>
          <div style="flex:1"></div>
          <button type="button" class="btn btn-secondary" id="modal-cancel-footer">Cancel</button>
          <button type="button" class="btn btn-primary" id="btn-next">Next ${icon.chevron}</button>
          <button type="submit" class="btn btn-primary" id="modal-submit" style="display:none">${icon.play} Deploy Application</button>
        </div>
      </form>
    </div>`;
}

function addEnvRow(modal, idx) {
  const row = document.createElement('div');
  row.className = 'env-row';
  row.id = `env-row-${idx}`;
  row.innerHTML = `
    <input class="input input-mono" placeholder="KEY" data-env-key />
    <input class="input input-mono" placeholder="value" data-env-val />
    <button type="button" class="btn-remove" onclick="this.closest('.env-row').remove()">${icon.trash}</button>`;
  modal.querySelector('#env-rows').appendChild(row);
}

async function handleDeploy(modal, form, onSuccess, close) {
  const errEl  = modal.querySelector('#modal-error');
  const submit = modal.querySelector('#modal-submit');

  errEl.style.display = 'none';
  submit.disabled = true;
  submit.innerHTML = `${spinner} Deploying…`;

  const env_vars = {};
  modal.querySelectorAll('.env-row').forEach(row => {
    const k = row.querySelector('[data-env-key]').value.trim();
    const v = row.querySelector('[data-env-val]').value;
    if (k) env_vars[k] = v;
  });

  const tokenId      = modal.querySelector('#f-token-id').value.trim();
  const dockerCpu    = parseFloat(modal.querySelector('#f-docker-cpu').value);
  const dockerMemory = parseInt(modal.querySelector('#f-docker-memory').value, 10);
  const dockerTmpfsSize = parseInt(modal.querySelector('#f-docker-tmpfs-size').value, 10);

  const payload = {
    name:            modal.querySelector('#f-name').value.trim(),
    repo_url:        modal.querySelector('#f-repo').value.trim(),
    ...(tokenId
      ? { github_token_id: tokenId }
      : { github_token: modal.querySelector('#f-token').value.trim() || null }),
    no_web:        modal.querySelector('.app-type-btn.active')?.dataset.type === 'worker',
    start_command: modal.querySelector('#f-cmd').value.trim() || null,
    port:          parseInt(modal.querySelector('#f-port').value) || null,
    docker_cpu_limit: Number.isFinite(dockerCpu) ? dockerCpu : null,
    docker_memory_limit_mb: Number.isInteger(dockerMemory) ? dockerMemory : null,
    docker_read_only_root: modal.querySelector('#f-docker-readonly').checked,
    docker_tmpfs_enabled: modal.querySelector('#f-docker-tmpfs-enabled').checked,
    docker_tmpfs_size_mb: Number.isInteger(dockerTmpfsSize) ? dockerTmpfsSize : null,
    env_vars,
  };

  try {
    const app = await api.deploy(payload);
    close();
    onSuccess(app);
  } catch (err) {
    errEl.textContent = err.message;
    errEl.style.display = 'block';
    submit.disabled = false;
    submit.innerHTML = `${icon.play} Deploy Application`;
  }
}
