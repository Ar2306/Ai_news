// AR front-end: one script for both pages (body[data-page] = "latest" | "archive").
// No framework, no third-party code. Every value from the data files is untrusted
// (it comes from RSS feeds and an LLM), so all text goes through esc() and every
// link through safeUrl() before it touches the DOM.
(() => {
  'use strict';

  const PAGE = document.body.dataset.page === 'archive' ? 'archive' : 'latest';
  const DATA_URL = PAGE === 'archive' ? 'data/archive.json' : 'data/newsletter.json';
  const STALE_HOURS = 30;
  const TAGS = [
    'llm', 'reinforcement-learning', 'world-models', 'foundational-models',
    'multimodal', 'robotics', 'interpretability', 'ai-safety',
    'simulation', 'training-infra', 'general-ml', 'other',
  ];
  const TAG_SET = new Set(TAGS);
  const GROUP_LABELS = {
    papers: 'Paper', labs: 'Lab', arxiv: 'arXiv', blogs: 'Blog',
    news: 'News', medium: 'Medium', community: 'HN', reddit: 'Reddit',
  };
  const VIEWS = PAGE === 'archive'
    ? [['all', 'All'], ['week', 'Week'], ['foryou', 'For you'], ['later', 'Read later']]
    : [['all', 'All'], ['foryou', 'For you'], ['new', 'New'], ['later', 'Read later']];
  const SORTS = ['important', 'newest', 'oldest', 'source'];

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
    const links = {};
    if (a.links && typeof a.links === 'object') {
      for (const k of ['pdf', 'code', 'hf']) {
        const u = safeUrl(a.links[k]);
        if (u !== '#') links[k] = u;
      }
    }
    const discussions = (Array.isArray(a.discussions) ? a.discussions : [])
      .filter((d) => d && typeof d === 'object' && safeUrl(d.url) !== '#')
      .slice(0, 6)
      .map((d) => ({ source: str(d.source, 60) || 'Discussion', url: safeUrl(d.url), points: Number.isFinite(d.points) ? d.points : 0 }));
    const importance = Number.isInteger(a.importance) && a.importance >= 1 && a.importance <= 5 ? a.importance : 3;
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
      importance,
      group: GROUP_LABELS[a.group] ? a.group : '',
      links,
      discussions,
    };
  }

  // ------------------------------------------------------------------- state

  const params = new URLSearchParams(location.search);
  const viewParam = { saved: 'later' }[params.get('view')] || params.get('view');
  const state = {
    q: (params.get('q') || '').slice(0, 100),
    tag: TAG_SET.has(params.get('tag')) ? params.get('tag') : 'all',
    source: (params.get('source') || '').slice(0, 120),
    view: VIEWS.some(([v]) => v === viewParam) ? viewParam : 'all',
    sort: SORTS.includes(params.get('sort')) ? params.get('sort') : 'important',
    showMuted: false,
  };

  let items = [];          // everything loaded for this page
  let days = [];           // archive only: [{date, count}]
  let generatedAt = '';
  let newSince = null;     // ISO timestamp of the last digest this browser saw
  let cursor = -1;         // keyboard-selected card index

  const cleanTerm = (t) => String(t || '').trim().replace(/\s+/g, ' ').slice(0, 60);
  const saved = loadSaved(store.get('ar.saved', {}));
  const read = loadRead(store.get('ar.read', {}));
  const prefs = loadPrefs(store.get('ar.prefs', {}));

  function loadSaved(raw) {
    const out = {};
    if (raw && typeof raw === 'object') {
      for (const v of Object.values(raw)) {
        const a = normalize(v, v && v.day);
        a.savedAt = v && Number.isFinite(v.savedAt) ? v.savedAt : 0;
        if (a.url !== '#') out[a.url] = a;
      }
    }
    return out;
  }

  function loadRead(raw) {
    const out = {};
    if (raw && typeof raw === 'object') {
      for (const [url, ts] of Object.entries(raw)) {
        if (safeUrl(url) === url && Number.isFinite(ts)) out[url] = ts;
      }
    }
    return out;
  }

  function loadPrefs(raw) {
    const list = (v) => Array.from(new Set((Array.isArray(v) ? v : []).map(cleanTerm).filter(Boolean))).slice(0, 50);
    const p = raw && typeof raw === 'object' ? raw : {};
    return { follow: list(p.follow), mute: list(p.mute), hideRead: p.hideRead === true };
  }

  const persistSaved = () => store.set('ar.saved', saved);
  const persistPrefs = () => store.set('ar.prefs', prefs);
  function persistRead() {
    // Keep the newest 3000 entries so storage never grows without bound
    const entries = Object.entries(read);
    if (entries.length > 3000) {
      entries.sort((a, b) => b[1] - a[1]).slice(3000).forEach(([u]) => delete read[u]);
    }
    store.set('ar.read', read);
  }

  function syncUrl() {
    const p = new URLSearchParams();
    if (state.q) p.set('q', state.q);
    if (state.tag !== 'all') p.set('tag', state.tag);
    if (state.source) p.set('source', state.source);
    if (state.view !== 'all') p.set('view', state.view);
    if (state.sort !== 'important') p.set('sort', state.sort);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }

  const isNew = (a) => !!newSince && !!a.fetched_at && a.fetched_at > newSince;
  const isRead = (a) => !!read[a.url];

  function termMatches(a, term) {
    const t = term.toLowerCase();
    return a.tags.includes(t)
      || a.source.toLowerCase() === t
      || (GROUP_LABELS[a.group] || '').toLowerCase() === t
      || `${a.title}\n${a.what}\n${a.why}`.toLowerCase().includes(t);
  }
  const isFollowed = (a) => prefs.follow.some((t) => termMatches(a, t));
  const isMuted = (a) => prefs.mute.some((t) => termMatches(a, t));

  function weekCutoff() {
    const d = new Date(Date.now() - 6 * 86400000);
    return d.toISOString().slice(0, 10);
  }

  // ----------------------------------------------------------------- filters

  function baseItems() {
    switch (state.view) {
      case 'later': return Object.values(saved);
      case 'new': return items.filter(isNew);
      case 'foryou': return items.filter(isFollowed);
      case 'week': { const cut = weekCutoff(); return items.filter((a) => a.day >= cut); }
      default: return items;
    }
  }

  // Read later is your own list, so read/mute filters never hide anything there.
  const personalFiltersApply = () => state.view !== 'later';

  function matches(a, { skipTag = false, skipSource = false, skipMute = false, skipRead = false } = {}) {
    if (!skipTag && state.tag !== 'all' && !a.tags.includes(state.tag)) return false;
    if (!skipSource && state.source && a.source !== state.source) return false;
    if (personalFiltersApply()) {
      if (!skipMute && !state.showMuted && isMuted(a)) return false;
      if (!skipRead && prefs.hideRead && isRead(a)) return false;
    }
    const q = state.q.trim().toLowerCase();
    if (q) {
      const hay = `${a.title}\n${a.what}\n${a.why}\n${a.who}\n${a.source}\n${a.author}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function sorted(list) {
    const out = list.slice();
    const byDate = (a, b) => (a.published_at === b.published_at ? 0 : (a.published_at > b.published_at ? -1 : 1));
    if (state.sort === 'source') {
      out.sort((a, b) => a.source.localeCompare(b.source) || byDate(a, b));
    } else if (state.sort === 'important') {
      out.sort((a, b) => b.importance - a.importance || byDate(a, b));
    } else if (state.view === 'later') {
      out.sort((a, b) => (state.sort === 'oldest' ? a.savedAt - b.savedAt : b.savedAt - a.savedAt));
    } else {
      out.sort((a, b) => (state.sort === 'oldest' ? -byDate(a, b) : byDate(a, b)));
    }
    return out;
  }

  // --------------------------------------------------------------- rendering

  const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`;
  const ICON_BOOKMARK = svg('M7 3.5h10a1 1 0 0 1 1 1V21l-6-4-6 4V4.5a1 1 0 0 1 1-1z');
  const ICON_CHECK = svg('M5 12.5l4.5 4.5L19 7.5');
  const ICON_LINK = svg('M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.2 1.2M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.2-1.2');
  const ICON_OUT = svg('M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5');
  const ICON_EYE = svg('M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z');
  const ICON_MUTE = svg('M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7c1.9 0 3.6-.6 5-1.5M9.9 9.9a3 3 0 0 0 4.2 4.2');

  function importanceMeter(n) {
    return `<span class="imp" title="Importance ${n}/5" aria-label="Importance ${n} of 5">${
      [1, 2, 3, 4, 5].map((i) => `<i class="${i <= n ? 'on' : ''}"></i>`).join('')}</span>`;
  }

  function renderExtras(a) {
    const links = [];
    if (a.links.pdf) links.push(`<a href="${esc(a.links.pdf)}" target="_blank" rel="noopener noreferrer" data-read="${esc(a.url)}">PDF</a>`);
    if (a.links.code) links.push(`<a href="${esc(a.links.code)}" target="_blank" rel="noopener noreferrer">Code</a>`);
    if (a.links.hf) links.push(`<a href="${esc(a.links.hf)}" target="_blank" rel="noopener noreferrer">HF Paper</a>`);
    a.discussions.forEach((d) => {
      links.push(`<a href="${esc(d.url)}" target="_blank" rel="noopener noreferrer" title="Discussion on ${esc(d.source)}">${esc(d.source)}${d.points ? ` · ${esc(d.points)} pts` : ''}</a>`);
    });
    return links.length ? `<div class="extras">${links.join('')}</div>` : '';
  }

  function renderCard(a, q) {
    const isSaved = !!saved[a.url];
    const followed = isFollowed(a);
    const readNow = isRead(a);
    const tags = a.tags
      .map((t) => `<button type="button" class="chip" data-t="${esc(t)}" data-action="tag" data-value="${esc(t)}">${esc(t)}</button>`)
      .join('');
    const row = (label, text, cls = '') =>
      text ? `<div class="sum-row ${cls}"><dt>${label}</dt><dd>${hl(text, q)}</dd></div>` : '';
    const when = fmtDate(a.published_at, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
    const laterView = state.view === 'later';

    return `
      <article class="card${readNow ? ' is-read' : ''}${followed ? ' is-followed' : ''}" data-id="${esc(a.url)}" data-t="${esc(a.tags[0])}" tabindex="-1">
        <div class="card-top">
          <div class="chips">${tags}</div>
          <div class="card-meta">
            ${followed ? '<span class="badge-follow">Following</span>' : ''}
            ${isNew(a) ? '<span class="badge-new">New</span>' : ''}
            ${laterView && a.savedAt ? `<span class="added">added ${esc(relTime(new Date(a.savedAt).toISOString()))}</span><span aria-hidden="true">·</span>` : ''}
            ${a.group ? `<span class="group">${esc(GROUP_LABELS[a.group])}</span><span aria-hidden="true">·</span>` : ''}
            <time datetime="${esc(a.published_at)}" title="${esc(when)}">${esc(relTime(a.published_at))}</time>
          </div>
        </div>
        <h3 class="card-title"><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer" data-read="${esc(a.url)}">${hl(a.title, q)}</a></h3>
        <dl class="sum">
          ${row('What', a.what)}
          ${row('Why', a.why)}
          ${row('Who', a.who, 'sum-who')}
        </dl>
        ${renderExtras(a)}
        <div class="card-foot">
          <div class="foot-left">
            ${importanceMeter(a.importance)}
            <button type="button" class="source-btn" data-action="source" data-value="${esc(a.source)}" title="Show only ${esc(a.source)}">
              <span class="source-dot" aria-hidden="true">${esc(a.source.charAt(0).toUpperCase())}</span><span class="source-name-inline">${hl(a.source, q)}</span>
            </button>
          </div>
          <div class="actions">
            ${laterView
              ? `<button type="button" class="later-btn done-btn" data-action="done" data-value="${esc(a.url)}" title="Finished reading — remove from Read later">${ICON_CHECK}<span>Done</span></button>`
              : `<button type="button" class="later-btn" data-action="save" data-value="${esc(a.url)}" aria-pressed="${isSaved}" title="${isSaved ? 'Remove from Read later' : 'Add to Read later'} (l)">${ICON_BOOKMARK}<span class="later-label">${isSaved ? 'In Read later' : 'Read later'}</span></button>`}
            <button type="button" class="icon-btn read-btn" data-action="read" data-value="${esc(a.url)}" aria-pressed="${readNow}" title="${readNow ? 'Mark as unread' : 'Mark as read'} (m)">${ICON_EYE}<span class="sr-only">Toggle read</span></button>
            <button type="button" class="icon-btn" data-action="mute-source" data-value="${esc(a.source)}" title="Mute ${esc(a.source)}">${ICON_MUTE}<span class="sr-only">Mute source</span></button>
            <button type="button" class="icon-btn" data-action="copy" data-value="${esc(a.url)}" title="Copy link">${ICON_LINK}<span class="sr-only">Copy link</span></button>
            <a class="icon-btn" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer" data-read="${esc(a.url)}" title="Open (o)">${ICON_OUT}<span class="sr-only">Open</span></a>
          </div>
        </div>
      </article>`;
  }

  function tagCounts(list) {
    const counts = {};
    list.forEach((a) => a.tags.forEach((t) => { counts[t] = (counts[t] || 0) + 1; }));
    return counts;
  }

  function renderHero() {
    const hero = $('#hero');
    const pool = state.view === 'week' ? baseItems() : items;
    const sources = new Set(pool.map((a) => a.source)).size;
    let kicker;
    let title;
    let meta;
    if (PAGE === 'archive' && state.view === 'week') {
      kicker = 'This week';
      title = 'The week in AI/ML';
      meta = [plural(pool.length, 'item'), plural(sources, 'source'), 'most important first'];
    } else if (PAGE === 'archive') {
      kicker = 'Archive';
      title = `${plural(days.length, 'day')} of AI/ML news`;
      const range = days.length
        ? `${fmtDate(days[days.length - 1].date + 'T12:00:00', { month: 'short', day: 'numeric' })} – ${fmtDate(days[0].date + 'T12:00:00', { month: 'short', day: 'numeric', year: 'numeric' })}`
        : '';
      meta = [plural(items.length, 'item'), plural(sources, 'source'), range];
    } else {
      kicker = 'Daily AI/ML digest';
      title = fmtDate(generatedAt, { weekday: 'long', month: 'long', day: 'numeric' }) || 'Latest';
      const unread = items.filter((a) => !isRead(a)).length;
      meta = [plural(items.length, 'item'), `${unread} unread`, plural(sources, 'source'), generatedAt ? `updated ${relTime(generatedAt)}` : ''];
    }

    const counts = tagCounts(pool);
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

  function renderToolbar() {
    const pool = baseItems().filter((a) => matches(a, { skipTag: true }));
    const counts = tagCounts(pool);
    const tags = TAGS.filter((t) => counts[t] || state.tag === t);
    $('#tag-filters').innerHTML =
      `<button type="button" class="filter" data-action="tag" data-value="all" aria-pressed="${state.tag === 'all'}">All <span class="n">${pool.length}</span></button>` +
      tags.map((t) => `<button type="button" class="filter" data-t="${t}" data-action="tag" data-value="${t}" aria-pressed="${state.tag === t}"><span class="dot" aria-hidden="true"></span>${t} <span class="n">${counts[t] || 0}</span></button>`).join('');

    const counters = {
      new: items.filter(isNew).length,
      later: Object.keys(saved).length,
      foryou: prefs.follow.length ? items.filter(isFollowed).length : '',
      week: '',
      all: '',
    };
    $('#view-filters').innerHTML = VIEWS
      .filter(([v]) => v !== 'new' || counters.new || state.view === 'new')
      .map(([v, label]) => `<button type="button" class="seg-btn" data-action="view" data-value="${v}" aria-pressed="${state.view === v}">${label}${counters[v] !== '' ? ` <span class="n">${counters[v]}</span>` : ''}</button>`)
      .join('');

    $('#sort').value = state.sort;
    const hide = $('#hide-read');
    hide.setAttribute('aria-pressed', String(prefs.hideRead));
    hide.disabled = state.view === 'later';
    const search = $('#search');
    if (document.activeElement !== search) search.value = state.q;
    updateLaterCount();
  }

  function renderSidebar() {
    const pool = baseItems().filter((a) => matches(a, { skipSource: true }));
    const counts = {};
    pool.forEach((a) => { counts[a.source] = (counts[a.source] || 0) + 1; });
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 12);
    const max = top.length ? top[0][1] : 1;
    $('#sources').innerHTML = top.length
      ? top.map(([name, n]) => `
          <li><button type="button" class="source-row" data-action="source" data-value="${esc(name)}" aria-pressed="${state.source === name}">
            <span class="source-name">${esc(name)}</span><span class="n">${n}</span>
            <span class="meter" data-w="${(n / max * 100).toFixed(1)}" aria-hidden="true"></span>
          </button></li>`).join('')
      : '<li class="muted">No sources</li>';
    $$('#sources .meter').forEach((el) => { el.style.width = `${el.dataset.w}%`; });

    const termList = (list, kind) => (list.length
      ? list.map((t) => `<button type="button" class="term" data-action="${kind}-remove" data-value="${esc(t)}" title="Remove">${esc(t)} <span aria-hidden="true">×</span></button>`).join('')
      : `<span class="muted small">${kind === 'follow' ? 'Nothing yet — try “robotics” or “agents”.' : 'Nothing muted.'}</span>`);
    $('#follow-list').innerHTML = termList(prefs.follow, 'follow');
    $('#mute-list').innerHTML = termList(prefs.mute, 'mute');
    $('#topic-suggest').innerHTML = TAGS.filter((t) => !prefs.follow.includes(t) && !prefs.mute.includes(t))
      .map((t) => `<button type="button" class="term ghost" data-action="follow-quick" data-value="${t}" title="Follow ${t}">+ ${t}</button>`).join('');

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

  function renderActiveFilters(shown, of, hiddenMuted, visibleUnread) {
    const bits = [];
    if (state.q) bits.push(['q', `“${state.q}”`]);
    if (state.tag !== 'all') bits.push(['tag', state.tag]);
    if (state.source) bits.push(['source', state.source]);
    if (state.view !== 'all') bits.push(['view', VIEWS.find(([v]) => v === state.view)[1].toLowerCase()]);
    $('#result-line').innerHTML = `
      <span>Showing <strong>${shown}</strong> of ${of}</span>
      ${bits.map(([k, label]) => `<button type="button" class="pill" data-action="clear" data-value="${k}" title="Remove filter">${esc(label)} <span aria-hidden="true">×</span></button>`).join('')}
      ${bits.length > 1 ? '<button type="button" class="link-btn" data-action="clear" data-value="all">Clear all</button>' : ''}
      <span class="result-spacer"></span>
      ${hiddenMuted ? `<button type="button" class="link-btn" data-action="show-muted">${state.showMuted ? 'Hide' : 'Show'} ${plural(hiddenMuted, 'muted item')}</button>` : ''}
      ${visibleUnread && state.view !== 'later' ? `<button type="button" class="link-btn" data-action="mark-all-read">Mark ${visibleUnread} as read</button>` : ''}`;
  }

  function renderFeed() {
    const q = state.q.trim().toLowerCase();
    const base = baseItems();
    const list = sorted(base.filter((a) => matches(a)));
    const hiddenMuted = personalFiltersApply() ? base.filter((a) => isMuted(a) && matches(a, { skipMute: true })).length : 0;
    const feed = $('#feed');
    cursor = -1;

    renderActiveFilters(list.length, base.length, hiddenMuted, list.filter((a) => !isRead(a)).length);

    if (!list.length) {
      let title = 'No items match';
      let hint = 'Try a different search or remove a filter.';
      let button = state.view !== 'all' || state.q || state.tag !== 'all' || state.source
        ? '<button type="button" class="btn" data-action="clear" data-value="all">Clear filters</button>' : '';
      if (state.view === 'later' && !base.length) {
        title = 'Your Read later list is empty';
        hint = 'Tap “Read later” on any item (or press l) and it stays here — even after the daily refresh — until you mark it Done.';
        button = '<button type="button" class="btn" data-action="view" data-value="all">Browse items</button>';
      } else if (state.view === 'foryou' && !prefs.follow.length) {
        title = 'Follow topics to build your feed';
        hint = 'Add topics, keywords or sources under “Your interests” — e.g. robotics, agents, DeepMind.';
        button = '<button type="button" class="btn" data-action="focus-interests">Add interests</button>';
      } else if (prefs.hideRead && base.some((a) => matches(a, { skipRead: true }))) {
        title = 'You’re all caught up';
        hint = 'Everything here is marked as read.';
        button = '<button type="button" class="btn" data-action="toggle-hide-read">Show read items</button>';
      }
      feed.innerHTML = `<div class="empty"><p class="empty-title">${title}</p><p class="muted">${hint}</p>${button}</div>`;
      return;
    }

    // Top picks: the day's most important items, shown first on the unfiltered Latest page
    const unfiltered = PAGE === 'latest' && state.view === 'all' && !q && state.tag === 'all' && !state.source && state.sort === 'important';
    let rest = list;
    let picksHtml = '';
    if (unfiltered) {
      const picks = list.filter((a) => a.importance >= 4).slice(0, 5);
      if (picks.length >= 2) {
        const ids = new Set(picks.map((a) => a.url));
        rest = list.filter((a) => !ids.has(a.url));
        picksHtml = `
          <section class="top-picks" aria-label="Top picks">
            <h2 class="section-head"><span>Top picks</span><span class="n">${picks.length} most important today</span></h2>
            ${picks.map((a) => renderCard(a, q)).join('')}
          </section>
          <h2 class="section-head"><span>Everything else</span><span class="n">${plural(rest.length, 'item')}</span></h2>`;
      }
    }

    if (PAGE === 'archive' && !['later', 'week'].includes(state.view)) {
      const groups = new Map();
      rest.forEach((a) => {
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
      feed.innerHTML = picksHtml + rest.map((a) => renderCard(a, q)).join('');
    }
  }

  function renderNav() {
    const current = PAGE === 'latest'
      ? (state.view === 'later' ? 'later' : 'latest')
      : ({ week: 'week', later: 'later' }[state.view] || 'archive');
    $$('.nav a').forEach((el) => el.setAttribute('aria-current', el.dataset.nav === current ? 'page' : 'false'));
  }

  function render() {
    renderHero();
    renderToolbar();
    renderSidebar();
    renderFeed();
    renderNav();
    syncUrl();
  }

  // ------------------------------------------------------------- interactions

  let toastTimer;
  function toast(msg, action) {
    const el = $('#toast');
    el.innerHTML = `<span>${esc(msg)}</span>${action ? `<button type="button" class="toast-action">${esc(action.label)}</button>` : ''}`;
    el.classList.toggle('has-action', !!action);
    if (action) $('.toast-action', el).addEventListener('click', () => { action.run(); el.classList.remove('show'); });
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), action ? 5000 : 1800);
  }

  function updateLaterCount() {
    const n = Object.keys(saved).length;
    $$('.later-count').forEach((el) => { el.textContent = n; el.hidden = n === 0; });
  }

  function toggleSave(url, { done = false } = {}) {
    if (saved[url]) {
      delete saved[url];
      toast(done ? 'Marked as done' : 'Removed from Read later');
      if (done) { read[url] = Date.now(); persistRead(); }
    } else {
      const a = items.find((x) => x.url === url);
      if (!a) return;
      saved[url] = { ...a, savedAt: Date.now() };
      toast('Added to Read later');
    }
    persistSaved();
    // Update in place so the list doesn't jump, except inside the Read later view
    if (state.view === 'later') {
      render();
    } else {
      $$('.later-btn').filter((b) => b.dataset.value === url).forEach((b) => {
        const on = !!saved[url];
        b.setAttribute('aria-pressed', String(on));
        b.title = `${on ? 'Remove from Read later' : 'Add to Read later'} (l)`;
        $('.later-label', b).textContent = on ? 'In Read later' : 'Read later';
      });
      renderToolbar();
    }
  }

  function setRead(url, value) {
    if (value) read[url] = Date.now(); else delete read[url];
    persistRead();
    $$('.card').filter((c) => c.dataset.id === url).forEach((c) => {
      c.classList.toggle('is-read', value);
      const b = $('.read-btn', c);
      if (b) { b.setAttribute('aria-pressed', String(value)); b.title = `${value ? 'Mark as unread' : 'Mark as read'} (m)`; }
    });
  }

  function addTerm(kind, raw) {
    const term = cleanTerm(raw);
    if (!term) return;
    const list = prefs[kind];
    const other = prefs[kind === 'follow' ? 'mute' : 'follow'];
    if (other.includes(term)) other.splice(other.indexOf(term), 1);
    if (!list.includes(term)) list.push(term);
    persistPrefs();
    render();
    toast(kind === 'follow' ? `Following “${term}”` : `Muted “${term}”`, {
      label: 'Undo',
      run: () => { list.splice(list.indexOf(term), 1); persistPrefs(); render(); },
    });
  }

  function removeTerm(kind, term) {
    const list = prefs[kind];
    if (list.includes(term)) list.splice(list.indexOf(term), 1);
    persistPrefs();
    render();
  }

  async function copyText(text, okMsg) {
    try {
      await navigator.clipboard.writeText(text);
      toast(okMsg);
    } catch {
      toast('Copy failed');
    }
  }

  // Sync: a portable code with Read later, interests and recent read history.
  function exportSync() {
    const recentRead = Object.fromEntries(Object.entries(read).sort((a, b) => b[1] - a[1]).slice(0, 800));
    const payload = JSON.stringify({ v: 1, saved, prefs, read: recentRead });
    const bytes = new TextEncoder().encode(payload);
    let bin = '';
    bytes.forEach((b) => { bin += String.fromCharCode(b); });
    copyText(`AR1.${btoa(bin)}`, 'Sync code copied — paste it on your other device');
  }

  function importSync(code) {
    code = (code || '').trim();
    if (!code) return;
    try {
      if (!code.startsWith('AR1.')) throw new Error('not an AR sync code');
      const bin = atob(code.slice(4));
      const data = JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0))));
      const inSaved = loadSaved(data.saved);
      const inRead = loadRead(data.read);
      const inPrefs = loadPrefs(data.prefs);
      Object.entries(inSaved).forEach(([u, a]) => { if (!saved[u]) saved[u] = a; });
      Object.entries(inRead).forEach(([u, ts]) => { read[u] = Math.max(read[u] || 0, ts); });
      prefs.follow = Array.from(new Set([...prefs.follow, ...inPrefs.follow])).slice(0, 50);
      prefs.mute = Array.from(new Set([...prefs.mute, ...inPrefs.mute])).filter((t) => !prefs.follow.includes(t)).slice(0, 50);
      persistSaved(); persistRead(); persistPrefs();
      render();
      toast(`Synced ${plural(Object.keys(inSaved).length, 'saved item')}`);
    } catch (e) {
      toast(`Couldn’t read that code (${e.message})`);
    }
  }

  function scrollToTop() {
    const feedTop = $('#top-of-feed').getBoundingClientRect().top + window.scrollY - 80;
    if (window.scrollY > feedTop) window.scrollTo({ top: feedTop });
  }

  // Opening an item marks it read (left, middle or keyboard click)
  function onOpen(e) {
    const link = e.target.closest('a[data-read]');
    if (link && (e.type === 'click' || e.button === 1)) setRead(link.dataset.read, true);
  }
  document.addEventListener('auxclick', onOpen);

  document.addEventListener('click', (e) => {
    onOpen(e);
    const nav = e.target.closest('.nav a[data-view]');
    if (nav && nav.dataset.page === PAGE && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      state.view = nav.dataset.view;
      render(); scrollToTop();
      return;
    }
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
      case 'save': toggleSave(value); break;
      case 'done': toggleSave(value, { done: true }); break;
      case 'read': setRead(value, !read[value]); renderToolbar(); break;
      case 'copy': copyText(value, 'Link copied'); break;
      case 'mute-source': addTerm('mute', value); break;
      case 'follow-quick': addTerm('follow', value); break;
      case 'follow-add':
      case 'mute-add': {
        const input = $('#interest-input');
        addTerm(action === 'follow-add' ? 'follow' : 'mute', input.value);
        input.value = '';
        break;
      }
      case 'follow-remove': removeTerm('follow', value); break;
      case 'mute-remove': removeTerm('mute', value); break;
      case 'show-muted': state.showMuted = !state.showMuted; render(); break;
      case 'toggle-hide-read':
        prefs.hideRead = !prefs.hideRead;
        persistPrefs();
        render();
        break;
      case 'mark-all-read': {
        const list = baseItems().filter((a) => matches(a) && !isRead(a));
        list.forEach((a) => { read[a.url] = Date.now(); });
        persistRead();
        render();
        toast(`Marked ${plural(list.length, 'item')} as read`, {
          label: 'Undo',
          run: () => { list.forEach((a) => delete read[a.url]); persistRead(); render(); },
        });
        break;
      }
      case 'focus-interests': $('#interest-input').focus(); break;
      case 'sync-export': exportSync(); break;
      case 'sync-import': importSync(window.prompt('Paste the sync code from your other device:')); break;
      default:
    }
  });

  $('#interest-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addTerm('follow', e.target.value);
      e.target.value = '';
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
    state.sort = SORTS.includes(e.target.value) ? e.target.value : 'important';
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
      case 'j': moveCursor(1); break;
      case 'k': moveCursor(-1); break;
      case 'o':
        if (current) {
          setRead(current.dataset.id, true);
          window.open(safeUrl(current.dataset.id), '_blank', 'noopener,noreferrer');
        }
        break;
      case 'm':
        if (current) { setRead(current.dataset.id, !read[current.dataset.id]); renderToolbar(); }
        break;
      case 'l':
      case 's':
        if (current) toggleSave(current.dataset.id, { done: state.view === 'later' });
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

  function showStaleBanner(iso) {
    const d = parseDate(iso);
    const banner = $('#banner');
    if (!d || !banner) return;
    const hours = (Date.now() - d) / 3600000;
    if (hours > STALE_HOURS) {
      banner.innerHTML = `Last update was <strong>${esc(relTime(iso))}</strong>. The daily refresh may have failed — <a href="data/status.json" target="_blank" rel="noopener">check status</a>.`;
      banner.hidden = false;
    }
  }

  async function loadStatus() {
    try {
      const res = await fetch('data/status.json', { cache: 'no-cache' });
      if (!res.ok) return;
      const st = await res.json();
      const ok = Number.isInteger(st.sources_ok) ? st.sources_ok : 0;
      const failed = Number.isInteger(st.sources_failed) ? st.sources_failed : 0;
      const by = st.summarized_by && typeof st.summarized_by === 'object' ? Object.entries(st.summarized_by) : [];
      const llm = by.filter(([k]) => k !== 'extractive fallback').sort((a, b) => b[1] - a[1])[0];
      const summary = llm ? `summaries by ${str(llm[0], 60)}` : (by.length ? 'summaries: excerpts (LLM unavailable)' : '');
      $('#status-line').textContent = [`${ok}/${ok + failed} sources OK`, summary].filter(Boolean).join(' · ');
    } catch { /* status is optional */ }
  }

  async function load() {
    try {
      const res = await fetch(DATA_URL, { cache: 'no-cache' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (PAGE === 'archive') {
        const archive = Array.isArray(data.archive) ? data.archive : [];
        const valid = archive
          .filter((d) => d && /^\d{4}-\d{2}-\d{2}$/.test(d.date) && Array.isArray(d.articles) && d.articles.length)
          .sort((a, b) => b.date.localeCompare(a.date));
        items = valid.flatMap((d) => d.articles.map((a) => normalize(a, d.date))).filter((a) => a.url !== '#');
        days = valid.map((d) => ({ date: d.date, count: d.articles.length }));
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

      showStaleBanner(generatedAt);
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
  loadStatus();
})();
