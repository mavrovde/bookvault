const state = { books: [], selected: new Set(), filter: '', typeFilter: 'all', sortBy: 'title-asc' };

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function formatSize(mb) {
  if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
  return mb.toFixed(1) + ' MB';
}

async function loadLibrary() {
  const listEl = document.getElementById('book-list');
  try {
    const resp = await fetch('/library');
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'failed to load');
    state.books = data.books;
    state.selected = new Set(data.books.map(b => b.id));
    renderList();
  } catch (e) {
    listEl.innerHTML = '<div class="empty-state" style="color:var(--danger)">Could not load your library.</div>';
  }
}

function visibleBooks() {
  let list = state.books;
  if (state.typeFilter === 'book') list = list.filter(b => !b.is_audio);
  else if (state.typeFilter === 'audio') list = list.filter(b => b.is_audio);
  if (state.filter) {
    const f = state.filter.toLowerCase();
    list = list.filter(b =>
      (b.title || '').toLowerCase().includes(f) || (b.authors || '').toLowerCase().includes(f)
    );
  }
  const sorted = list.slice();
  const collate = (a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' });
  switch (state.sortBy) {
    case 'title-desc': sorted.sort((a, b) => collate(b.title || '', a.title || '')); break;
    case 'author-asc': sorted.sort((a, b) => collate(a.authors || '', b.authors || '')); break;
    case 'size-desc': sorted.sort((a, b) => (b.size_mb ?? -1) - (a.size_mb ?? -1)); break;
    case 'size-asc': sorted.sort((a, b) => (a.size_mb ?? Infinity) - (b.size_mb ?? Infinity)); break;
    default: sorted.sort((a, b) => collate(a.title || '', b.title || '')); break; // title-asc
  }
  return sorted;
}

function bookCardHtml(b) {
  const cover = b.cover_url
    ? `<img class="book-cover" src="${escapeHtml(b.cover_url)}" alt="" loading="lazy">`
    : `<span class="book-cover placeholder">${b.is_audio ? '🎧' : '📖'}</span>`;
  const typeDot = `<span class="book-type-dot" title="${b.is_audio ? 'Audiobook' : 'E-book'}">${b.is_audio ? '🎧' : '📖'}</span>`;
  const sizeText = b.size_mb != null ? `${b.size_mb} MB` : '';
  const selected = state.selected.has(b.id);
  return `
    <label class="book-card ${selected ? 'selected' : ''}" data-row="${b.id}">
      <div class="book-cover-wrap">
        ${cover}
        <span class="book-checkbox"><input type="checkbox" data-id="${b.id}" ${selected ? 'checked' : ''}></span>
        ${typeDot}
      </div>
      <span class="book-title-g" title="${escapeHtml(b.title)}">${escapeHtml(b.title)}</span>
      ${b.authors ? `<span class="book-authors-g" title="${escapeHtml(b.authors)}">${escapeHtml(b.authors)}</span>` : ''}
      <span class="book-size-g" id="size-${b.id}">${sizeText}</span>
    </label>
  `;
}

async function fetchSizesInBackground() {
  // Sequential on purpose -- the backend has a single dedicated
  // worker thread (Playwright thread-affinity, see session.py), so
  // "parallel" fetches here would just queue up behind each other
  // anyway. Runs after the list is already visible/interactive.
  for (const b of state.books) {
    if (b.size_mb != null) continue;
    try {
      const resp = await fetch(`/library/${b.id}/size`);
      const data = await resp.json();
      if (data.ok) {
        b.size_mb = data.size_mb;
        const el = document.getElementById(`size-${b.id}`);
        if (el && b.size_mb != null) el.textContent = `${b.size_mb} MB`;
        if (state.selected.has(b.id)) updateSelectedCount();
      }
    } catch (e) {
      // best-effort -- leave this row's size blank on failure
    }
  }
  // Sizes load lazily and can change size-based sort order -- only
  // worth a full re-render for that sort mode, once all sizes are in.
  if (state.sortBy.startsWith('size')) renderList();
}

function renderList() {
  const listEl = document.getElementById('book-list');
  const books = visibleBooks();
  if (state.books.length === 0) {
    listEl.innerHTML = '<div class="empty-state">No books found.</div>';
    updateSelectedCount();
    return;
  }
  if (books.length === 0) {
    listEl.innerHTML = '<div class="empty-state">No titles match your filter.</div>';
    updateSelectedCount();
    return;
  }
  listEl.innerHTML = books.map(bookCardHtml).join('');
  listEl.querySelectorAll('input[type=checkbox]').forEach(cb => {
    cb.addEventListener('change', () => {
      const id = Number(cb.dataset.id);
      if (cb.checked) state.selected.add(id); else state.selected.delete(id);
      cb.closest('.book-card').classList.toggle('selected', cb.checked);
      updateSelectedCount();
    });
  });
  updateSelectedCount();
}

function updateSelectedCount() {
  const n = state.selected.size;
  document.getElementById('selected-count').textContent = `${n} of ${state.books.length} selected`;

  let sumMb = 0, unknown = 0;
  for (const b of state.books) {
    if (!state.selected.has(b.id)) continue;
    if (b.size_mb != null) sumMb += b.size_mb;
    else unknown += 1;
  }
  // `unknown` books haven't had their size fetched yet (sizes load
  // lazily in the background, see fetchSizesInBackground) -- say so
  // explicitly, since a bare number here previously read as
  // unexplained "estimating" noise.
  const sizeSummary = n === 0 ? '' : `(~${formatSize(sumMb)} so far${unknown > 0 ? `, size of ${unknown} more still loading…` : ''})`;
  document.getElementById('selected-size').textContent = sizeSummary;

  const bar = document.getElementById('selection-bar');
  bar.style.display = n > 0 ? 'flex' : 'none';
  document.getElementById('bar-count').textContent = `${n} book${n === 1 ? '' : 's'} selected`;
  document.getElementById('bar-size').textContent = sizeSummary;
}

document.getElementById('search-box').addEventListener('input', (e) => {
  state.filter = e.target.value;
  renderList();
});

document.getElementById('type-filter').addEventListener('click', (e) => {
  const btn = e.target.closest('.pill');
  if (!btn) return;
  state.typeFilter = btn.dataset.type;
  document.querySelectorAll('#type-filter .pill').forEach(p => p.classList.toggle('active', p === btn));
  renderList();
});

document.getElementById('sort-by').addEventListener('change', (e) => {
  state.sortBy = e.target.value;
  renderList();
});

document.getElementById('select-all').addEventListener('click', () => {
  visibleBooks().forEach(b => state.selected.add(b.id));
  renderList();
});
document.getElementById('select-none').addEventListener('click', () => {
  visibleBooks().forEach(b => state.selected.delete(b.id));
  renderList();
});

document.getElementById('start-download').addEventListener('click', async () => {
  if (state.selected.size === 0) return;
  const btn = document.getElementById('start-download');
  btn.disabled = true;
  try {
    const resp = await fetch('/download/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        art_ids: Array.from(state.selected),
        ebook_format: document.getElementById('ebook-format').value,
        audiobook_format: document.getElementById('audiobook-format').value,
      }),
    });
    const data = await resp.json();
    if (!data.ok) {
      alert('Could not start download: ' + (data.error || 'unknown error'));
      return;
    }
    document.getElementById('progress-section').style.display = 'block';
    document.getElementById('progress-section').scrollIntoView({ behavior: 'smooth' });
    pollStatus();
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('cancel-download').addEventListener('click', async () => {
  await fetch('/download/cancel', { method: 'POST' });
});

async function pollStatus() {
  const resp = await fetch('/download/status');
  const s = await resp.json();
  renderProgress(s);
  if (s.status === 'running') {
    setTimeout(pollStatus, 1000);
  }
}

function renderProgress(s) {
  document.getElementById('progress-section').style.display = 'block';
  const labels = { idle: 'Idle', running: 'Downloading…', done: 'Done', error: 'Error', cancelled: 'Stopped' };
  const badge = document.getElementById('progress-badge');
  badge.textContent = labels[s.status] || s.status;
  badge.className = 'badge badge-' + s.status;

  const total = s.total != null ? s.total : (state.selected.size || null);
  document.getElementById('progress-count').textContent = total ? `${s.done} / ${total} books` : `${s.done} books`;

  const bar = document.getElementById('progress-bar');
  if (total) {
    bar.classList.remove('indeterminate');
    bar.style.width = Math.min(100, (s.done / total) * 100) + '%';
  } else if (s.status === 'running') {
    bar.classList.add('indeterminate');
  } else {
    bar.classList.remove('indeterminate');
    bar.style.width = s.status === 'done' ? '100%' : '0%';
  }

  document.getElementById('progress-current').textContent =
    s.current_title ? `Downloading: ${s.current_title}` : '';

  const logEl = document.getElementById('progress-log');
  logEl.innerHTML = s.log.map(item => {
    if (item.status === 'skipped') {
      return `<li class="skipped"><span class="icon">!</span><span class="title">${escapeHtml(item.title)}</span><span class="detail">${escapeHtml(item.reason || 'Skipped -- no file available')}</span></li>`;
    }
    if (item.status === 'error') {
      return `<li class="error"><span class="icon">✗</span><span class="title">${escapeHtml(item.title)}</span><span class="detail" title="${escapeHtml(item.detail || '')}">${escapeHtml(item.error || 'Download failed')}</span></li>`;
    }
    return `<li class="done"><span class="icon">✓</span><span class="title">${escapeHtml(item.title)}</span><span class="detail">${item.ext}, ${item.size_mb} MB</span></li>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;

  document.getElementById('cancel-download').style.display = s.status === 'running' ? 'inline-block' : 'none';
  document.getElementById('download-link').style.display = (s.status === 'done' || s.status === 'cancelled') && s.done > 0 ? 'inline-block' : 'none';
  document.getElementById('progress-error').textContent = s.error || '';
}

(async function init() {
  await loadLibrary();
  fetchSizesInBackground();
  const resp = await fetch('/download/status');
  const s = await resp.json();
  if (s.status === 'running' || s.status === 'done' || s.status === 'cancelled') {
    renderProgress(s);
    if (s.status === 'running') pollStatus();
  }
})();
