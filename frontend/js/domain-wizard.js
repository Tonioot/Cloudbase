// "Connect a domain" wizard: domain → DNS record → free HTTPS certificate.
//
//   openDomainWizard(app, { onDone(updatedApp), domain? })

import { api } from './api.js';
import { toast } from './utils.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const STEPS = ['Domain', 'DNS', 'HTTPS'];

// Name of the app's Let's Encrypt certificate (from /etc/letsencrypt/live/<name>/…), if it has one
export function leCertName(app) {
  const m = /^\/etc\/letsencrypt\/live\/([a-z0-9.-]+)\/fullchain\.pem$/.exec(app?.ssl_cert_path || '');
  return m ? m[1] : null;
}

const ICON = {
  check: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  copy: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  lock: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
  ext: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></svg>',
};

// Rough split into "record name" and zone: shop.example.com → shop / example.com.
// Good enough for the common case; two-part public suffixes (co.uk) get a hint.
function splitDomain(domain) {
  const parts = domain.split('.');
  const twoPartTld = /^(co|com|org|net|ac|gov)\.[a-z]{2}$/.test(parts.slice(-2).join('.'));
  const zoneLen = twoPartTld ? 3 : 2;
  if (parts.length <= zoneLen) return { name: '@', zone: domain, apex: true };
  return { name: parts.slice(0, parts.length - zoneLen).join('.'), zone: parts.slice(-zoneLen).join('.'), apex: false };
}

export function openDomainWizard(app, { onDone, domain = '' } = {}) {
  // With a domain given (e.g. "Set up HTTPS" for an existing one) start at the DNS check
  const state = {
    step: domain ? 1 : 0,
    domain,
    www: false,         // opt-in: only when www.<domain> also has a DNS record
    role: 'alias',      // for an app that already has a domain: 'alias' (show the app) | 'redirect'
    server: null,       // { ip, https_available, https_reason, email }
    check: null,        // last /check result
    checking: false,
    pollTimer: null,
    certPaths: null,
  };

  const backdrop = document.createElement('div');
  backdrop.className = 'dialog-backdrop dw-backdrop';
  backdrop.innerHTML = `
    <div class="dw" role="dialog" aria-modal="true" aria-labelledby="dw-title">
      <header class="dw-head">
        <div>
          <div class="dw-title" id="dw-title">Connect a domain</div>
          <div class="dw-steps" id="dw-steps"></div>
        </div>
        <button type="button" class="btn btn-icon btn-ghost" data-close aria-label="Close">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </header>
      <div class="dw-body" id="dw-body"></div>
      <footer class="dw-foot" id="dw-foot"></footer>
    </div>`;
  document.body.appendChild(backdrop);

  const $ = sel => backdrop.querySelector(sel);
  const close = () => {
    clearInterval(state.pollTimer);
    backdrop.remove();
  };
  backdrop.addEventListener('mousedown', e => { if (e.target === backdrop) close(); });
  backdrop.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  $('[data-close]').onclick = close;

  api.getDomainServer().then(s => { state.server = s; render(); }).catch(() => { state.server = { ip: null, https_available: false, https_reason: 'Could not read server details.' }; render(); });

  const allNames = () => {
    const names = [state.domain];
    if (state.www && splitDomain(state.domain).apex) names.push(`www.${state.domain}`);
    return names;
  };

  function renderSteps() {
    $('#dw-steps').innerHTML = STEPS.map((label, i) => {
      const cls = i < state.step ? 'done' : i === state.step ? 'active' : '';
      return `<span class="dw-step ${cls}"><span class="dw-step-n">${i < state.step ? ICON.check : i + 1}</span>${label}</span>`;
    }).join('<span class="dw-step-sep"></span>');
  }

  function footer(html) { $('#dw-foot').innerHTML = html; }

  function render() {
    renderSteps();
    clearInterval(state.pollTimer);
    if (state.step === 0) renderDomain();
    else if (state.step === 1) renderDns();
    else if (state.step === 2) renderHttps();
    else renderDone();
  }

  /* ── 1. Domain ───────────────────────────────────────────────────────── */
  function renderDomain() {
    $('#dw-body').innerHTML = `
      <label class="dw-label" for="dw-domain">Which domain should open <strong>${esc(app.name)}</strong>?</label>
      <input class="input dw-input" id="dw-domain" placeholder="app.example.com" value="${esc(state.domain)}" autocomplete="off" spellcheck="false" />
      <div class="dw-hint" id="dw-domain-hint">You need to own this domain. A subdomain like <code>app.example.com</code> works too.</div>
      <label class="dw-check" id="dw-www-row" hidden>
        <input type="checkbox" id="dw-www" ${state.www ? 'checked' : ''} />
        <span>Also send <code id="dw-www-name"></code> to this app</span>
      </label>
      ${app.domain ? `
        <div class="dw-label dw-role-label">What should this domain do?</div>
        <div class="choice-list dw-role">
          <label class="choice">
            <input type="radio" name="dw-role" value="alias" ${state.role === 'alias' ? 'checked' : ''} />
            <span class="choice-dot"></span>
            <span class="choice-text"><span>Show the app</span><small>Same site as ${esc(app.domain)}, on this address too</small></span>
          </label>
          <label class="choice">
            <input type="radio" name="dw-role" value="redirect" ${state.role === 'redirect' ? 'checked' : ''} />
            <span class="choice-dot"></span>
            <span class="choice-text"><span>Redirect to ${esc(app.domain)}</span><small>Visitors are sent to the primary domain</small></span>
          </label>
        </div>` : ''}`;
    footer(`<button class="btn" data-close>Cancel</button><button class="btn btn-primary" id="dw-next" disabled>Continue</button>`);
    $('#dw-foot [data-close]').onclick = close;

    const input = $('#dw-domain');
    const next = $('#dw-next');
    const sync = () => {
      const v = input.value.trim().toLowerCase().replace(/^[a-z]+:\/\//, '').split('/')[0].replace(/\.$/, '');
      const valid = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(v);
      next.disabled = !valid;
      const apex = valid && splitDomain(v).apex;
      $('#dw-www-row').hidden = !apex;
      if (apex) $('#dw-www-name').textContent = `www.${v}`;
      state.domain = valid ? v : '';
    };
    input.addEventListener('input', sync);
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !next.disabled) next.click(); });
    $('#dw-www').onchange = e => { state.www = e.target.checked; };
    backdrop.querySelectorAll('input[name="dw-role"]').forEach(r => { r.onchange = () => { state.role = r.value; }; });
    next.onclick = () => { state.step = 1; state.check = null; render(); };
    sync();
    setTimeout(() => input.focus(), 30);
  }

  /* ── 2. DNS ──────────────────────────────────────────────────────────── */
  function renderDns() {
    const { name, zone } = splitDomain(state.domain);
    const ip = state.server?.ip;
    const rows = [{ type: 'A', name, value: ip }];
    if (state.www && splitDomain(state.domain).apex) rows.push({ type: 'A', name: 'www', value: ip });

    $('#dw-body').innerHTML = `
      <p class="dw-lead">Add this record at the company where you bought <strong>${esc(zone)}</strong> (the DNS settings of your domain).</p>
      <div class="dw-records">
        <div class="dw-rec dw-rec-head"><span>Type</span><span>Name</span><span>Value</span></div>
        ${rows.map(r => `
          <div class="dw-rec">
            <span class="dw-mono">${r.type}</span>
            <span class="dw-copyable" data-copy="${esc(r.name)}"><span class="dw-mono">${esc(r.name)}</span>${ICON.copy}</span>
            <span class="dw-copyable" data-copy="${esc(r.value || '')}"><span class="dw-mono">${esc(r.value || 'IP unknown')}</span>${ICON.copy}</span>
          </div>`).join('')}
      </div>
      <div class="dw-hint">Leave <em>TTL</em> on its default. Using Cloudflare? The proxy (orange cloud) can stay on as long as <em>Always Use HTTPS</em> is off. If the check below stays red, set the record to <em>DNS only</em> (grey) for a moment.</div>
      <div class="dw-status" id="dw-status"></div>`;

    backdrop.querySelectorAll('.dw-copyable').forEach(el => {
      el.onclick = () => navigator.clipboard.writeText(el.dataset.copy).then(() => toast('Copied')).catch(() => {});
    });

    footer(`<button class="btn" id="dw-back">Back</button><span class="dw-foot-gap"></span><button class="btn btn-ghost" id="dw-skip">Skip check</button><button class="btn btn-primary" id="dw-next" disabled>Continue</button>`);
    $('#dw-back').onclick = () => { state.step = 0; render(); };
    $('#dw-skip').onclick = () => { state.step = 2; render(); };
    $('#dw-next').onclick = () => { state.step = 2; render(); };

    runCheck();
    state.pollTimer = setInterval(() => { if (!state.check?.reachable && !state.check?.nginx_ready) runCheck(); }, 8000);
  }

  async function runCheck() {
    if (state.checking || state.step !== 1) return;
    state.checking = true;
    paintStatus();
    try {
      state.check = await api.checkDomain(state.domain);
    } catch (e) {
      state.check = { dns_ok: false, reachable: false, detail: e.message };
    } finally {
      state.checking = false;
      if (state.step === 1) paintStatus();
    }
  }

  function paintStatus() {
    const el = $('#dw-status');
    if (!el) return;
    const c = state.check;
    let tone = 'wait', title = 'Waiting for the DNS record…', detail = 'Checking every few seconds. Changes usually show up within minutes.';
    if (c?.reachable) {
      tone = 'ok'; title = `${state.domain} reaches this server`; detail = '';
    } else if (c?.cloudflare_proxy) {
      tone = 'warn'; title = 'Cloudflare proxy is on'; detail = c.detail;
    } else if (c?.dns_ok && c?.nginx_ready) {
      tone = 'ok'; title = `${state.domain} points here and nginx is ready`; detail = c.detail;
    } else if (c?.dns_ok) {
      tone = 'warn'; title = 'DNS is set, but the test didn’t pass'; detail = c.detail;
    } else if (c?.ips?.length) {
      tone = 'warn'; title = 'The record points somewhere else'; detail = c.detail;
    } else if (c?.detail && c.detail !== 'No DNS record found yet. New records can take a few minutes to appear.') {
      tone = 'warn'; title = 'Couldn’t check the domain'; detail = c.detail;
    }
    el.className = `dw-status dw-status--${tone}`;
    el.innerHTML = `
      <span class="dw-status-icon">${tone === 'ok' ? ICON.check : '<span class="dw-pulse"></span>'}</span>
      <span class="dw-status-text"><strong>${esc(title)}</strong>${detail ? `<span>${esc(detail)}</span>` : ''}${(c?.warnings || []).map(w => `<span class="dw-warn-line">${esc(w)}</span>`).join('')}</span>
      ${tone !== 'ok' ? `<button class="btn btn-sm" id="dw-recheck" ${state.checking ? 'disabled' : ''}>${state.checking ? 'Checking…' : 'Check now'}</button>` : ''}`;
    $('#dw-recheck')?.addEventListener('click', runCheck);
    const next = $('#dw-next');
    if (next) next.disabled = !(c?.reachable || c?.dns_ok);
    const skip = $('#dw-skip');
    if (skip) skip.hidden = !!(c?.reachable || c?.nginx_ready);
  }

  /* ── 3. HTTPS ────────────────────────────────────────────────────────── */
  function renderHttps() {
    const s = state.server || {};
    const names = certNames();
    if (s.https_available) {
      $('#dw-body').innerHTML = `
        <div class="dw-https">
          <span class="dw-https-icon">${ICON.lock}</span>
          <div>
            <div class="dw-https-title">Free HTTPS certificate</div>
            <div class="dw-hint">From Let’s Encrypt, renewed automatically. Covers ${names.map(n => `<code>${esc(n)}</code>`).join(', ')}.</div>
          </div>
        </div>
        <label class="dw-label" for="dw-email">Email for your Let’s Encrypt account <span class="dw-optional">optional · for important notices about your certificates</span></label>
        <input class="input dw-input" id="dw-email" type="email" placeholder="you@example.com" value="${esc(s.email || '')}" />
        <div class="dw-status" id="dw-https-status" hidden></div>`;
      footer(`<button class="btn" id="dw-back">Back</button><span class="dw-foot-gap"></span><button class="btn btn-ghost" id="dw-nohttps">Use HTTP only</button><button class="btn btn-primary" id="dw-issue">Set up HTTPS</button>`);
      $('#dw-issue').onclick = issue;
    } else {
      $('#dw-body').innerHTML = `
        <div class="dw-https dw-https--off">
          <span class="dw-https-icon">${ICON.lock}</span>
          <div>
            <div class="dw-https-title">Automatic HTTPS isn’t available on this server yet</div>
            <div class="dw-hint">${esc(s.https_reason || '')}</div>
          </div>
        </div>
        <p class="dw-lead">You can connect the domain over HTTP now and add HTTPS later — or upload your own certificate in the app’s Network settings.</p>`;
      footer(`<button class="btn" id="dw-back">Back</button><span class="dw-foot-gap"></span><button class="btn btn-primary" id="dw-nohttps">Connect over HTTP</button>`);
    }
    $('#dw-back').onclick = () => { state.step = 1; render(); };
    $('#dw-nohttps').onclick = () => finish(null);
  }

  // A certificate is per app, so it must cover every name the app answers on
  function certNames() {
    const existing = [app.domain, ...(app.extra_domains || []), ...(app.redirect_domains || [])].filter(Boolean);
    return [...new Set([...allNames(), ...existing])];
  }

  async function issue() {
    const btn = $('#dw-issue');
    const status = $('#dw-https-status');
    const email = $('#dw-email').value.trim();
    btn.disabled = true;
    $('#dw-back').disabled = true;
    $('#dw-nohttps').disabled = true;
    btn.textContent = 'Requesting…';
    status.hidden = false;
    status.className = 'dw-status dw-status--wait';
    status.innerHTML = `<span class="dw-status-icon"><span class="dw-pulse"></span></span><span class="dw-status-text"><strong>Requesting certificate</strong><span>Let’s Encrypt is verifying the domain. This takes about 10–30 seconds.</span></span>`;
    try {
      // Update the app's existing certificate rather than adding a second one
      const res = await api.requestCertificate(certNames(), email || null, leCertName(app));
      await finish({ cert: res.ssl_cert_path, key: res.ssl_key_path });
    } catch (e) {
      status.className = 'dw-status dw-status--warn';
      status.innerHTML = `<span class="dw-status-icon"><span class="dw-pulse"></span></span><span class="dw-status-text"><strong>That didn’t work</strong><span>${esc(e.message)}</span></span>`;
      btn.disabled = false;
      $('#dw-back').disabled = false;
      $('#dw-nohttps').disabled = false;
      btn.textContent = 'Try again';
    }
  }

  /* ── Save on the app ─────────────────────────────────────────────────── */
  async function finish(cert) {
    const names = allNames();
    const primary = app.domain || names[0];
    const extras = [...(app.extra_domains || [])];
    const redirects = [...(app.redirect_domains || [])];
    for (const n of names) {
      if (n === primary || extras.includes(n) || redirects.includes(n)) continue;
      // Chosen to redirect, or www.<domain>: send visitors to the primary
      // instead of serving a duplicate site
      if (app.domain && state.role === 'redirect') redirects.push(n);
      else if (n.startsWith('www.') && names.includes(n.slice(4))) redirects.push(n);
      else extras.push(n);
    }
    const payload = { domain: primary, extra_domains: extras, redirect_domains: redirects };
    if (cert) {
      payload.ssl_cert_path = cert.cert;
      payload.ssl_key_path = cert.key;
    }
    try {
      const updated = await api.updateApp(app.id, payload);
      state.https = !!cert;
      state.step = 3;
      render();
      onDone?.(updated);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  /* ── Done ────────────────────────────────────────────────────────────── */
  function renderDone() {
    $('#dw-steps').innerHTML = '';
    const url = `${state.https ? 'https' : 'http'}://${state.domain}`;
    $('#dw-body').innerHTML = `
      <div class="dw-done">
        <span class="dw-done-icon">${ICON.check}</span>
        <div class="dw-done-title">${esc(state.domain)} is connected</div>
        <div class="dw-hint">${state.https ? 'HTTPS is on and renews automatically.' : 'Served over HTTP. Run this wizard again later to add HTTPS.'}</div>
        <a class="dw-done-link" href="${esc(url)}" target="_blank" rel="noopener">${esc(url.replace(/^https?:\/\//, ''))} ${ICON.ext}</a>
      </div>`;
    footer(`<span class="dw-foot-gap"></span><button class="btn btn-primary" data-close>Done</button>`);
    $('#dw-foot [data-close]').onclick = close;
  }

  render();
}
