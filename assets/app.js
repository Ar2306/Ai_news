// AR front-end: one script for both pages (body[data-page] = "latest" | "archive").
// No framework, no third-party code. Every value from the data files is untrusted
// (it comes from RSS feeds and an LLM), so all text goes through esc() and every
// link through safeUrl() before it touches the DOM.
(() => {
  'use strict';

  const PAGE = document.body.dataset.page === 'archive' ? 'archive' : 'latest';
  const DATA_URL = PAGE === 'archive' ? 'data/archive.json' : 'data/newsletter.json';
  const TAGS = [
    'llm', 'reinforcement-learning', 'world-models', 'foundational-models',
    'multimodal', 'robotics', 'interpretability', 'ai-safety',
    'simulation', 'training-infra', 'general-ml', 'other',
  ];
  const TAG_SET = new Set(TAGS);

  // ---------------------------------------------------------------- utilities

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ESC[c]);

  function safeUrl(url) {
    try {
      const u = new URL(String(url));
      return u.protocol === 'https:' || u.protocol === 'http:' ? u.href : '#';
    } catch {
      return '#';
    }
  }

  const str = (v, max = 2000) => (typeof v === 'string' ? v : '').slice(0, max);

  // localStorage can be missing or throw (private mode, blocked storage).
  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem(key);
        return raw == null ? fallback : JSON.parse(raw);
      } catch {
        return fallback;
      }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
    },
  };

  function parseDate(iso) {
    const d = new Date(iso);
    return isNaN(d) ? null : d;
  }

  function relTime(iso) {
    const d = parseDate(iso);
    if (!d) return '';
    const mins = Math.round((Date.now() - d) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.round(hrs / 24);
    if (days < 7) return `${days}d ago`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }

  function fmtDate(iso, opts) {
    const d = parseDate(iso);
    return d ? d.toLocaleDateString('en-US', opts) : '';
  }

  // Highlight query matches without ever inserting unescaped text.
  function hl(text, q) {
    const s = String(text == null ? '' : text);
    if (!q) return esc(s);
    const lower = s.toLowerCase();
    let out = '';
    let i = 0;
    for (let j = lower.indexOf(q, i); j !== -1; j = lower.indexOf(q, i)) {
      out += esc(s.slice(i, j)) + '<mark>' + esc(s.slice(j, j + q.length)) + '</mark>';
      i = j + q.length;
    }
    return out + esc(s.slice(i));
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  // Coerce one raw article into a known-safe shape.
  function normalize(raw, day) {
    const a = raw && typeof raw === 'object' ? raw : {};
    let s = a.summary;
    if (typeof s === 'string') s = { what: s };
    if (!s || typeof s !== 'object') s = {};
    const tags = (Array.isArray(a.tags) ? a.tags : [])
      .filter((t) => typeof t === 'string')
      .map((t) => (TAG_SET.has(t) ? t : 'other'));
    const url = safeUrl(a.url);
    return {
      id: url,
      url,
      title: str(a.title, 400) || '(untitled)',
      source: str(a.source, 120) || 'Unknown source',
      author: str(a.author, 300),
      tags: tags.length ? Array.from(new Set(tags)) : ['other'],
      what: str(s.what, 600),
      why: str(s.why, 600),
      who: str(s.who, 300),
      published_at: str(a.published_at, 40),
      fetched_at: str(a.fetched_at, 40),
      day: day || str(a.day, 10),
    };
  }

  // ------------------------------------------------------------------- state

  const params = new URLSearchParams(location.search);
  const state = {
    q: (params.get('q') || '').slice(0, 100),
    tag: TAG_SET.has(params.get('tag')) ? params.get('tag') : 'all',
    source: (params.get('source') || '').slice(0, 120),
    view: ['saved', 'new'].includes(params.get('view')) ? params.get('view') : 'all',
    sort: ['oldest', 'source'].includes(params.get('sort')) ? params.get('sort') : 'newest',
  };

  let items = [];          // everything loaded for this page
  let days = [];           // archive only: [{date, count}]
  let generatedAt = '';
  let newSince = null;     // ISO timestamp of the last digest this browser saw
  let cursor = -1;         // keyboard-selected card index

  const saved = (() => {
    const raw = store.get('ar.saved', {});
    const out = {};
    if (raw && typeof raw === 'object') {
      for (const v of Object.values(raw)) {
        const a = normalize(v);
        if (a.url !== '#') out[a.url] = a;
      }
    }
    return out;
  })();

  function persistSaved() {
    store.set('ar.saved', saved);
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (state.q) p.set('q', state.q);
    if (state.tag !== 'all') p.set('tag', state.tag);
    if (state.source) p.set('source', state.source);
    if (state.view !== 'all') p.set('view', state.view);
    if (state.sort !== 'newest') p.set('sort', state.sort);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  const isNew = (a) => !!newSince && !!a.fetched_at && a.fetched_at > newSince;

  // ----------------------------------------------------------------- filters

  function baseItems() {
    if (state.view === 'saved') return Object.values(saved);
    if (state.view === 'new') return items.filter(isNew);
    return items;
  }

  function matches(a, { skipTag = false, skipSource = false } = {}) {
    if (!skipTag && state.tag !== 'all' && !a.tags.includes(state.tag)) return false;
    if (!skipSource && state.source && a.source !== state.source) return false;
    const q = state.q.trim().toLowerCase();
    if (q) {
      const hay = `${a.title}\n${a.what}\n${a.why}\n${a.who}\n${a.source}\n${a.author}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function sorted(list) {
    const out = list.slice();
    if (state.sort === 'source') {
      out.sort((a, b) => a.source.localeCompare(b.source) || (b.published_at > a.published_at ? 1 : -1));
    } else {
      const dir = state.sort === 'oldest' ? 1 : -1;
      out.sort((a, b) => (a.published_at === b.published_at ? 0 : (a.published_at > b.published_at ? dir : -dir)));
    }
    return out;
  }

  // --------------------------------------------------------------- rendering

  const ICON_STAR = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z"/></svg>';
  const ICON_LINK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.2 1.2M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.2-1.2"/></svg>';
  const ICON_OUT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>';

  function renderCard(a, q) {
    const isSaved = !!saved[a.url];
    const tags = a.tags
      .map((t) => `<button type="button" class="chip" data-t="${esc(t)}" data-action="tag" data-value="${esc(t)}">${esc(t)}</button>`)
      .join('');
    const row = (label, text, cls = '') =>
      text ? `<div class="sum-row ${cls}"><dt>${label}</dt><dd>${hl(text, q)}</dd></div>` : '';
    const when = fmtDate(a.published_at, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });

    return `
      <article class="card" data-id="${esc(a.url)}" data-t="${esc(a.tags[0])}" tabindex="-1">
        <div class="card-top">
          <div class="chips">${tags}</div>
          <div class="card-meta">
            ${isNew(a) ? '<span class="badge-new">New</span>' : ''}
            <time datetime="${esc(a.published_at)}" title="${esc(when)}">${esc(relTime(a.published_at))}</time>
          </div>
        </div>
        <h3 class="card-title"><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${hl(a.title, q)}</a></h3>
        <dl class="sum">
          ${row('What', a.what)}
          ${row('Why', a.why)}
          ${row('Who', a.who, 'sum-who')}
        </dl>
        <div class="card-foot">
          <button type="button" class="source-btn" data-action="source" data-value="${esc(a.source)}" title="Show only ${esc(a.source)}">
            <span class="source-dot" aria-hidden="true">${esc(a.source.charAt(0).toUpperCase())}</span>${hl(a.source, q)}
          </button>
          <div class="actions">
            <button type="button" class="icon-btn save-btn" data-action="save" data-value="${esc(a.url)}" aria-pressed="${isSaved}" title="${isSaved ? 'Remove from saved' : 'Save for later'} (s)">${ICON_STAR}<span class="sr-only">Save</span></button>
            <button type="button" class="icon-btn" data-action="copy" data-value="${esc(a.url)}" title="Copy link">${ICON_LINK}<span class="sr-only">Copy link</span></button>
            <a class="icon-btn" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer" title="Open source (o)">${ICON_OUT}<span class="sr-only">Open</span></a>
          </div>
        </div>
      </article>`;
  }

  function renderHero() {
    const hero = $('#hero');
    const sources = new Set(items.map((a) => a.source)).size;
    const kicker = PAGE === 'archive' ? 'Archive' : 'Daily AI/ML digest';
    let title;
    let meta;
    if (PAGE === 'archive') {
      title = `${plural(days.length, 'day')} of AI/ML news`;
      const range = days.length
        ? `${fmtDate(days[days.length - 1].date + 'T12:00:00', { month: 'short', day: 'numeric' })} – ${fmtDate(days[0].date + 'T12:00:00', { month: 'short', day: 'numeric', year: 'numeric' })}`
        : '';
      meta = [plural(items.length, 'item'), plural(sources, 'source'), range];
    } else {
      title = fmtDate(generatedAt, { weekday: 'long', month: 'long', day: 'numeric' }) || 'Latest';
      meta = [plural(items.length, 'item'), plural(sources, 'source'), generatedAt ? `updated ${relTime(generatedAt)}` : ''];
    }

    const counts = tagCounts(items);
    const total = Object.values(counts).reduce((s, n) => s + n, 0) || 1;
    const segments = TAGS.filter((t) => counts[t])
      .map((t) => `<button type="button" class="seg" data-t="${t}" data-action="tag" data-value="${t}" data-w="${(counts[t] / total * 100).toFixed(2)}" title="${t}: ${counts[t]}"><span class="sr-only">${t} ${counts[t]}</span></button>`)
      .join('');

    hero.innerHTML = `
      <p class="kicker">${esc(kicker)}</p>
      <h1>${esc(title)}</h1>
      <p class="hero-meta">${meta.filter(Boolean).map(esc).join('<span aria-hidden="true"> · </span>')}</p>
      ${segments ? `<div class="tagbar" role="group" aria-label="Topic mix — click to filter">${segments}</div>` : ''}`;
    // Widths via CSSOM (allowed by the strict CSP, unlike inline style attributes)
    $$('.seg', hero).forEach((el) => { el.style.flexGrow = el.dataset.w; });
  }

  function tagCounts(list) {
    const counts = {};
    list.forEach((a) => a.tags.forEach((t) => { counts[t] = (counts[t] || 0) + 1; }));
    return counts;
  }

  function renderToolbar() {
    const pool = baseItems().filter((a) => matches(a, { skipTag: true }));
    const counts = tagCounts(pool);
    const tags = TAGS.filter((t) => counts[t] || state.tag === t);
    $('#tag-filters').innerHTML =
      `<button type="button" class="filter" data-action="tag" data-value="all" aria-pressed="${state.tag === 'all'}">All <span class="n">${pool.length}</span></button>` +
      tags.map((t) => `<button type="button" class="filter" data-t="${t}" data-action="tag" data-value="${t}" aria-pressed="${state.tag === t}"><span class="dot" aria-hidden="true"></span>${t} <span class="n">${counts[t] || 0}</span></button>`).join('');

    const savedCount = Object.keys(saved).length;
    const newCount = items.filter(isNew).length;
    $('#view-filters').innerHTML = [
      ['all', 'Everything', ''],
      ['new', 'New', newCount],
      ['saved', 'Saved', savedCount],
    ]
      .filter(([v, , n]) => v === 'all' || v === state.view || n)
      .map(([v, label, n]) => `<button type="button" class="seg-btn" data-action="view" data-value="${v}" aria-pressed="${state.view === v}">${label}${n !== '' ? ` <span class="n">${n}</span>` : ''}</button>`)
      .join('');

    $('#sort').value = state.sort;
    const search = $('#search');
    if (document.activeElement !== search) search.value = state.q;
  }

  function renderSidebar() {
    const pool = baseItems().filter((a) => matches(a, { skipSource: true }));
    const counts = {};
    pool.forEach((a) => { counts[a.source] = (counts[a.source] || 0) + 1; });
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 10);
    const max = top.length ? top[0][1] : 1;
    $('#sources').innerHTML = top.length
      ? top.map(([name, n]) => `
          <li><button type="button" class="source-row" data-action="source" data-value="${esc(name)}" aria-pressed="${state.source === name}">
            <span class="source-name">${esc(name)}</span><span class="n">${n}</span>
            <span class="meter" data-w="${(n / max * 100).toFixed(1)}" aria-hidden="true"></span>
          </button></li>`).join('')
      : '<li class="muted">No sources</li>';
    $$('#sources .meter').forEach((el) => { el.style.width = `${el.dataset.w}%`; });

    const daysList = $('#days');
    if (daysList) {
      const visible = {};
      baseItems().filter((a) => matches(a)).forEach((a) => { visible[a.day] = (visible[a.day] || 0) + 1; });
      daysList.innerHTML = days.map((d) => `
        <li><a href="#day-${esc(d.date)}" class="day-link${visible[d.date] ? '' : ' is-empty'}">
          <span>${esc(fmtDate(d.date + 'T12:00:00', { weekday: 'short', month: 'short', day: 'numeric' }))}</span>
          <span class="n">${visible[d.date] || 0}</span>
        </a></li>`).join('');
    }
  }

  function renderActiveFilters(shown, of) {
    const bits = [];
    if (state.q) bits.push(['q', `“${state.q}”`]);
    if (state.tag !== 'all') bits.push(['tag', state.tag]);
    if (state.source) bits.push(['source', state.source]);
    if (state.view !== 'all') bits.push(['view', state.view]);
    $('#result-line').innerHTML = `
      <span>Showing <strong>${shown}</strong> of ${of}</span>
      ${bits.map(([k, label]) => `<button type="button" class="pill" data-action="clear" data-value="${k}" title="Remove filter">${esc(label)} <span aria-hidden="true">×</span></button>`).join('')}
      ${bits.length > 1 ? '<button type="button" class="link-btn" data-action="clear" data-value="all">Clear all</button>' : ''}`;
  }

  function renderFeed() {
    const q = state.q.trim().toLowerCase();
    const base = baseItems();
    const list = sorted(base.filter((a) => matches(a)));
    const feed = $('#feed');
    cursor = -1;

    renderActiveFilters(list.length, base.length);

    if (!list.length) {
      feed.innerHTML = `
        <div class="empty">
          <p class="empty-title">${state.view === 'saved' && !base.length ? 'Nothing saved yet' : 'No items match'}</p>
          <p class="muted">${state.view === 'saved' && !base.length ? 'Use the star on any item to keep it here.' : 'Try a different search or remove a filter.'}</p>
          ${state.view !== 'all' || state.q || state.tag !== 'all' || state.source ? '<button type="button" class="btn" data-action="clear" data-value="all">Clear filters</button>' : ''}
        </div>`;
      return;
    }

    if (PAGE === 'archive' && state.view !== 'saved') {
      const groups = new Map();
      list.forEach((a) => {
        if (!groups.has(a.day)) groups.set(a.day, []);
        groups.get(a.day).push(a);
      });
      const order = Array.from(groups.keys()).sort((a, b) => (state.sort === 'oldest' ? a.localeCompare(b) : b.localeCompare(a)));
      feed.innerHTML = order.map((day) => `
        <section class="day" id="day-${esc(day)}" aria-label="${esc(day)}">
          <h2 class="day-head"><span>${esc(fmtDate(day + 'T12:00:00', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }) || day)}</span><span class="n">${plural(groups.get(day).length, 'item')}</span></h2>
          ${groups.get(day).map((a) => renderCard(a, q)).join('')}
        </section>`).join('');
    } else {
      feed.innerHTML = list.map((a) => renderCard(a, q)).join('');
    }
  }

  function render() {
    renderToolbar();
    renderSidebar();
    renderFeed();
    syncUrl();
  }

  // ------------------------------------------------------------- interactions

  let toastTimer;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 1800);
  }

  function toggleSave(url) {
    if (saved[url]) {
      delete saved[url];
      toast('Removed from saved');
    } else {
      const a = items.find((x) => x.url === url);
      if (!a) return;
      saved[url] = a;
      toast('Saved');
    }
    persistSaved();
    // Update in place so the list doesn't jump, except in the Saved view
    if (state.view === 'saved') {
      render();
    } else {
      $$(`.save-btn`).filter((b) => b.dataset.value === url).forEach((b) => b.setAttribute('aria-pressed', String(!!saved[url])));
      renderToolbar();
    }
  }

  async function copyLink(url) {
    try {
      await navigator.clipboard.writeText(url);
      toast('Link copied');
    } catch {
      toast('Copy failed');
    }
  }

  function scrollToTop() {
    const feedTop = $('#top-of-feed').getBoundingClientRect().top + window.scrollY - 80;
    if (window.scrollY > feedTop) window.scrollTo({ top: feedTop });
  }

  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el) return;
    const { action, value } = el.dataset;
    switch (action) {
      case 'tag':
        state.tag = state.tag === value ? 'all' : value;
        render(); scrollToTop();
        break;
      case 'source':
        state.source = state.source === value ? '' : value;
        render(); scrollToTop();
        break;
      case 'view':
        state.view = value;
        render(); scrollToTop();
        break;
      case 'clear':
        if (value === 'all') Object.assign(state, { q: '', tag: 'all', source: '', view: 'all' });
        else if (value === 'q') state.q = '';
        else if (value === 'tag') state.tag = 'all';
        else if (value === 'source') state.source = '';
        else if (value === 'view') state.view = 'all';
        render();
        break;
      case 'save':
        toggleSave(value);
        break;
      case 'copy':
        copyLink(value);
        break;
      default:
    }
  });

  let searchTimer;
  $('#search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.q = e.target.value.slice(0, 100);
      render();
    }, 120);
  });

  $('#sort').addEventListener('change', (e) => {
    state.sort = e.target.value;
    render();
  });

  function moveCursor(delta) {
    const cards = $$('.card');
    if (!cards.length) return;
    cursor = Math.max(0, Math.min(cards.length - 1, cursor + delta));
    cards.forEach((c, i) => c.classList.toggle('is-current', i === cursor));
    cards[cursor].focus({ preventScroll: true });
    cards[cursor].scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  }

  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const typing = e.target.closest('input, textarea, select, [contenteditable]');
    const search = $('#search');
    if (e.key === 'Escape') {
      if (typing === search) {
        if (search.value) { search.value = ''; state.q = ''; render(); } else { search.blur(); }
      }
      return;
    }
    if (typing) return;
    const current = $$('.card')[cursor];
    switch (e.key) {
      case '/':
        e.preventDefault(); search.focus(); search.select();
        break;
      case 'j':
        moveCursor(1);
        break;
      case 'k':
        moveCursor(-1);
        break;
      case 'o':
        if (current) window.open(safeUrl(current.dataset.id), '_blank', 'noopener,noreferrer');
        break;
      case 's':
        if (current) toggleSave(current.dataset.id);
        break;
      default:
    }
  });

  // Sticky offsets (day headers, sidebar, scroll targets) follow the real toolbar height.
  const toolbar = $('.toolbar');
  const setToolbarHeight = () =>
    document.documentElement.style.setProperty('--toolbar-h', `${Math.ceil(toolbar.getBoundingClientRect().height)}px`);
  if ('ResizeObserver' in window) new ResizeObserver(setToolbarHeight).observe(toolbar);
  setToolbarHeight();

  // -------------------------------------------------------------------- load

  async function load() {
    try {
      const res = await fetch(DATA_URL, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (PAGE === 'archive') {
        const archive = Array.isArray(data.archive) ? data.archive : [];
        days = archive
          .filter((d) => d && /^\d{4}-\d{2}-\d{2}$/.test(d.date) && Array.isArray(d.articles) && d.articles.length)
          .map((d) => ({ date: d.date, articles: d.articles }))
          .sort((a, b) => b.date.localeCompare(a.date));
        items = days.flatMap((d) => d.articles.map((a) => normalize(a, d.date))).filter((a) => a.url !== '#');
        days = days.map((d) => ({ date: d.date, count: d.articles.length }));
        generatedAt = str(data.generated_at, 40);
        newSince = store.get('ar.lastSeen', null);
      } else {
        items = (Array.isArray(data.all_articles) ? data.all_articles : []).map((a) => normalize(a)).filter((a) => a.url !== '#');
        generatedAt = str(data.generated_at, 40);
        const lastSeen = store.get('ar.lastSeen', null);
        newSince = typeof lastSeen === 'string' && lastSeen < generatedAt ? lastSeen : null;
        if (generatedAt) store.set('ar.lastSeen', generatedAt);
      }
      // A stale ?view=new link with nothing new would show an empty page
      if (state.view === 'new' && !items.some(isNew)) state.view = 'all';

      renderHero();
      render();
      if (location.hash) {
        const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
        if (target) target.scrollIntoView();
      }
    } catch (err) {
      $('#hero').innerHTML = '<p class="kicker">Something went wrong</p><h1>Couldn’t load the digest</h1>';
      $('#feed').innerHTML = `<div class="empty"><p class="empty-title">${esc(err.message)}</p><button type="button" class="btn" id="retry">Try again</button></div>`;
      $('#retry').addEventListener('click', () => location.reload());
    }
  }

  load();
})();
