// Guides: in-panel documentation at /guides and /guides?g=<slug>.
// Content lives in guides-content.js; this file renders navigation, the
// article, its table of contents and previous/next links.

import { GUIDES, GUIDE_GROUPS } from './guides-content.js';
import { setCrumbs } from './shell.js';
import { toast } from './utils.js';

const esc = s => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const slugify = s => s.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

export function initGuides() {
  const slug = new URLSearchParams(location.search).get('g');
  const guide = GUIDES.find(g => g.slug === slug);
  renderNav(guide?.slug);
  if (guide) renderGuide(guide);
  else renderIndex();
}

function renderNav(activeSlug) {
  const nav = document.getElementById('guides-nav');
  const groups = GUIDE_GROUPS.map(group => {
    const items = GUIDES.filter(g => g.group === group);
    return `
      <div class="guides-nav-group">
        <div class="guides-nav-label">${esc(group)}</div>
        ${items.map(g => `<a href="/guides?g=${g.slug}" data-search="${esc(`${g.title} ${g.summary} ${g.keywords || ''}`.toLowerCase())}"${g.slug === activeSlug ? ' class="active" aria-current="page"' : ''}>${esc(g.title)}</a>`).join('')}
      </div>`;
  }).join('');

  nav.innerHTML = `
    <input class="guides-search" id="guides-search" type="search" placeholder="Search guides" aria-label="Search guides" autocomplete="off" />
    <select class="input guides-select" id="guides-select" aria-label="Choose a guide">
      <option value="">All guides</option>
      ${GUIDE_GROUPS.map(group => `<optgroup label="${esc(group)}">${GUIDES.filter(g => g.group === group).map(g => `<option value="${g.slug}"${g.slug === activeSlug ? ' selected' : ''}>${esc(g.title)}</option>`).join('')}</optgroup>`).join('')}
    </select>
    <div class="guides-nav-groups">${groups}<div class="guides-nav-empty" hidden>No guides match.</div></div>`;

  document.getElementById('guides-select').onchange = e => {
    location.href = e.target.value ? `/guides?g=${e.target.value}` : '/guides';
  };

  const search = document.getElementById('guides-search');
  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    nav.querySelectorAll('.guides-nav-group').forEach(groupEl => {
      let groupShown = 0;
      groupEl.querySelectorAll('a').forEach(a => {
        const hit = !q || a.dataset.search.includes(q);
        a.hidden = !hit;
        if (hit) groupShown++;
      });
      groupEl.hidden = !groupShown;
      shown += groupShown;
    });
    nav.querySelector('.guides-nav-empty').hidden = shown > 0;
  });
  search.addEventListener('keydown', e => {
    if (e.key === 'Enter') nav.querySelector('.guides-nav-group a:not([hidden])')?.click();
  });
}

function renderIndex() {
  setCrumbs(['Guides']);
  document.title = 'Guides — Cloudbase';
  document.getElementById('guide-toc').innerHTML = '';
  document.getElementById('guide').innerHTML = `
    <div class="guide-eyebrow">Documentation</div>
    <h1>Guides</h1>
    <p class="guide-lead">Everything about running apps with Cloudbase — from the first deploy to multi-server setups, domains and troubleshooting. New here? Start with <a href="/guides?g=how-it-works">How Cloudbase works</a>.</p>
    ${GUIDE_GROUPS.map(group => `
      <section class="guide-index-group">
        <h2>${esc(group)}</h2>
        <div class="guide-cards">
          ${GUIDES.filter(g => g.group === group).map(g => `
            <a class="guide-card" href="/guides?g=${g.slug}">
              <span class="guide-card-title">${esc(g.title)}</span>
              <span class="guide-card-sub">${esc(g.summary)}</span>
            </a>`).join('')}
        </div>
      </section>`).join('')}`;
}

function renderGuide(guide) {
  setCrumbs([{ label: 'Guides', href: '/guides' }, guide.title]);
  document.title = `${guide.title} — Cloudbase guides`;

  const idx = GUIDES.indexOf(guide);
  const prev = GUIDES[idx - 1];
  const next = GUIDES[idx + 1];

  const article = document.getElementById('guide');
  article.innerHTML = `
    <div class="guide-eyebrow">${esc(guide.group)}</div>
    <h1>${esc(guide.title)}</h1>
    <p class="guide-lead">${guide.lead || esc(guide.summary)}</p>
    ${guide.body}
    <nav class="guide-pager" aria-label="More guides">
      ${prev ? `<a class="prev" href="/guides?g=${prev.slug}"><small>Previous</small><span>${esc(prev.title)}</span></a>` : ''}
      ${next ? `<a class="next" href="/guides?g=${next.slug}"><small>Next</small><span>${esc(next.title)}</span></a>` : ''}
    </nav>`;

  // Code blocks get a copy button
  article.querySelectorAll('pre').forEach(pre => {
    const wrap = document.createElement('div');
    wrap.className = 'guide-code';
    pre.replaceWith(wrap);
    wrap.appendChild(pre);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'copy';
    btn.textContent = 'Copy';
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(pre.innerText.trim());
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1400);
      } catch { toast('Could not copy', 'error'); }
    };
    wrap.appendChild(btn);
  });

  // Table of contents from the h2 headings
  const heads = [...article.querySelectorAll('h2')];
  heads.forEach(h => { if (!h.id) h.id = slugify(h.textContent); });
  const toc = document.getElementById('guide-toc');
  toc.innerHTML = heads.length
    ? `<div class="guide-toc-label">On this page</div>${heads.map(h => `<a href="#${h.id}" data-id="${h.id}">${esc(h.textContent)}</a>`).join('')}`
    : '';

  const scroller = document.getElementById('guides-scroll');
  const links = [...toc.querySelectorAll('a')];
  const markActive = () => {
    let current = heads[0]?.id;
    for (const h of heads) if (h.getBoundingClientRect().top < 140) current = h.id;
    links.forEach(a => a.classList.toggle('active', a.dataset.id === current));
  };
  scroller.addEventListener('scroll', markActive, { passive: true });
  markActive();

  if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
}
