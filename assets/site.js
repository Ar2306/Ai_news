// Shared rendering + tag filtering for index.html and archive.html. No framework.

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url || '') ? url : '#';
}

function formatDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function summaryOf(article) {
  const s = article.summary;
  if (s && typeof s === 'object') return s;
  return { what: s || '', why: '', who: '' };
}

function renderArticle(article) {
  const tags = article.tags || [];
  const s = summaryOf(article);
  const line = (label, text) =>
    text ? `<p><span class="label">${label}</span>${escapeHtml(text)}</p>` : '';
  const meta = [article.source, formatDate(article.published_at)].filter(Boolean).map(escapeHtml).join(' · ');
  return `
    <article class="item" data-tags="${escapeHtml(tags.join(' '))}">
      <div class="tags">${tags.map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('')}</div>
      <h3><a href="${escapeHtml(safeUrl(article.url))}" target="_blank" rel="noopener">${escapeHtml(article.title)}</a></h3>
      ${line('What', s.what)}
      ${line('Why', s.why)}
      ${line('Who', s.who)}
      <p class="meta">${meta}</p>
    </article>`;
}

// Build "all" + one button per tag found in `articles` (most common first).
// `onChange(tag)` receives 'all' or the selected tag.
function renderTagFilters(container, articles, onChange) {
  const counts = {};
  articles.forEach(a => (a.tags || []).forEach(t => { counts[t] = (counts[t] || 0) + 1; }));
  const tags = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));

  const button = (tag, n) =>
    `<button type="button" data-tag="${escapeHtml(tag)}" aria-pressed="${tag === 'all'}">${escapeHtml(tag)}<span class="count">${n}</span></button>`;
  container.innerHTML = button('all', articles.length) + tags.map(t => button(t, counts[t])).join('');

  container.onclick = (e) => {
    const btn = e.target.closest('button[data-tag]');
    if (!btn) return;
    container.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    onChange(btn.dataset.tag);
  };
}

// Show only .item elements under `root` carrying `tag`; returns how many are visible.
function applyTagFilter(root, tag) {
  let visible = 0;
  root.querySelectorAll('.item').forEach(el => {
    const match = tag === 'all' || el.dataset.tags.split(' ').includes(tag);
    el.hidden = !match;
    if (match) visible++;
  });
  return visible;
}
