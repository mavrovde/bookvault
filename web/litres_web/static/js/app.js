// The frontend is a thin renderer. It owns no activity/progress logic: the
// backend runs a single state machine (see litres_web/activity.py) with states
// idle | refreshing | checking | preparing | stopping and a terminal
// `result` (done | cancelled | error). This file just:
//   1. dispatches user actions to the backend (refresh / prepare / cancel),
//   2. polls GET /activity and paints whatever state it reports,
//   3. renders the book list / filters / selection (pure display state).
// Every enable/disable/label rule below is a pure function of the backend's
// reported `state` -- there is no client-side notion of "what's running."
const state = { books: [], selected: new Set(), filter: '', typeFilter: 'all', sortBy: 'title-asc' };

// The last activity state the backend reported. Buttons derive purely from
// this (plus, for Prepare, the selection count), so it's cached here for the
// selection handlers that re-evaluate buttons between polls.
let currentState = 'idle';

const BUSY_STATES = new Set(['refreshing', 'checking', 'preparing', 'stopping']);

// Every button's enabled/label state is a pure function of the backend
// `state` (plus selection count for Prepare) -- recomputed as a whole rather
// than each action patching the buttons it thinks it affects.
function updateButtons() {
  const busy = BUSY_STATES.has(currentState);
  document.getElementById('refresh-library').disabled = busy;
  document.getElementById('start-download').disabled = busy || state.selected.size === 0;

  const cancelBtn = document.getElementById('cancel-download');
  const stoppable = currentState === 'checking' || currentState === 'preparing';
  cancelBtn.disabled = !stoppable;
  cancelBtn.textContent = currentState === 'stopping' ? 'Stopping…' : 'Stop';
}

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function formatSize(mb) {
  if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
  return mb.toFixed(1) + ' MB';
}

async function loadLibrary(forceRefresh) {
  const listEl = document.getElementById('book-list');
  try {
    const resp = await fetch(forceRefresh ? '/library?refresh=true' : '/library');
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'failed to load');
    state.books = data.books;
    // Nothing pre-selected: selecting everything by default made it easy
    // to kick off a full-library download by accident, and made every page
    // load look like "select all, then immediately query every book" --
    // exactly the repeated/bulk request pattern anti-bot checks flag.
    state.selected = new Set();
    renderList();
  } catch (e) {
    // A transient failure must not wipe a list that's already on screen --
    // e.g. the single worker thread was briefly busy with a download when the
    // library cache had expired. Keep the current list if we have one;
    // otherwise show a loading note and retry shortly (the backend serves a
    // stale list while busy, so this converges without user action).
    if (state.books.length > 0) {
      renderList();
    } else {
      listEl.innerHTML = '<div class="empty-state">Loading your library…</div>';
      setTimeout(() => loadLibrary(forceRefresh), 3000);
    }
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
      if (cb.checked) { state.selected.add(id); } else { state.selected.delete(id); }
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
  // `unknown` books haven't had their size resolved yet (the backend's
  // CHECKING sweep fills them in) -- say so explicitly, since a bare number
  // here previously read as unexplained "estimating" noise.
  const sizeSummary = n === 0 ? '' : `(~${formatSize(sumMb)} so far${unknown > 0 ? `, size of ${unknown} more still loading…` : ''})`;
  document.getElementById('selected-size').textContent = sizeSummary;

  updateButtons();
}

// Merge sizes resolved by the backend's CHECKING sweep into the local book
// list and paint each row. `sizes` is {id: mb|null}; a null means the book
// has no downloadable file (its row just stays blank).
function applySizes(sizes) {
  if (!sizes) return;
  let changed = false;
  for (const [idStr, mb] of Object.entries(sizes)) {
    const id = Number(idStr);
    const b = state.books.find(x => x.id === id);
    if (b && b.size_mb == null && mb != null) { b.size_mb = mb; changed = true; }
    const el = document.getElementById(`size-${id}`);
    if (el && mb != null) el.textContent = `${mb} MB`;
  }
  if (changed) {
    updateSelectedCount();
    // Sizes can change size-based sort order -- only worth a re-render for
    // those sort modes.
    if (state.sortBy.startsWith('size')) renderList();
  }
}

// -- Rendering the activity state ------------------------------------------
// One function maps the backend snapshot onto the shared progress card. It
// never mounts/unmounts anything -- the card is always on screen (see
// index.html) so switching between states reads as the same component
// updating rather than something popping in and out.
const BADGE = {
  refreshing: ['Refreshing…', 'badge-running'],
  checking: ['Checking sizes…', 'badge-running'],
  preparing: ['Building zip…', 'badge-running'],
  stopping: ['Stopping…', 'badge-running'],
};
const RESULT_BADGE = {
  done: ['Done', 'badge-done'],
  cancelled: ['Stopped', 'badge-cancelled'],
  error: ['Error', 'badge-error'],
};

function renderActivity(s) {
  const badge = document.getElementById('progress-badge');
  const [label, cls] = s.state === 'idle'
    ? (RESULT_BADGE[s.result] || ['Idle', 'badge-idle'])
    : (BADGE[s.state] || ['Idle', 'badge-idle']);
  badge.textContent = label;
  badge.className = 'badge ' + cls;

  const bar = document.getElementById('progress-bar');
  const countEl = document.getElementById('progress-count');
  const currentEl = document.getElementById('progress-current');

  // Progress bar + count line, per state.
  bar.classList.remove('indeterminate');
  if (s.state === 'refreshing' || s.state === 'stopping') {
    bar.classList.add('indeterminate');
    countEl.textContent = '';
  } else if (s.state === 'checking') {
    bar.style.width = s.total ? Math.min(100, (s.done / s.total) * 100) + '%' : '0%';
    countEl.textContent = s.total ? `${s.done} / ${s.total} sizes checked` : '';
  } else if (s.state === 'preparing') {
    // The bar reflects BYTES, not just whole books: blend the file currently
    // downloading into the book count as a fraction, so a single-book job (or
    // the last book of any job) visibly fills mid-transfer instead of sitting
    // at 0% until that one file finishes.
    const frac = (s.current_total && s.current_downloaded != null)
      ? Math.min(1, s.current_downloaded / s.current_total)
      : 0;
    if (s.total) {
      bar.style.width = Math.min(100, ((s.done + frac) / s.total) * 100) + '%';
      countEl.textContent = `${s.done} / ${s.total} books`;
    } else if (s.current_total) {
      // Whole-library job: book count is unknown, so fill by the current
      // file's byte progress -- at least the bar moves per book.
      bar.style.width = Math.min(100, frac * 100) + '%';
      countEl.textContent = `${s.done} books`;
    } else {
      bar.classList.add('indeterminate');
      countEl.textContent = s.done ? `${s.done} books` : '';
    }
  } else { // idle
    bar.style.width = s.result === 'done' ? '100%' : '0%';
    countEl.textContent = '';
  }

  // Current line: a book title while preparing, the backend's message
  // otherwise (which also carries the "what just happened" summary at idle).
  if (s.state === 'preparing' && s.current_title) {
    // Live MB for the file currently downloading. current_downloaded/_total
    // are bytes (total may be null if the server sent no Content-Length and
    // the size was unknown) -- show "12.3 / 45.0 MB", or just "12.3 MB" when
    // the total isn't known.
    let line = `Fetching: ${s.current_title}`;
    if (s.current_downloaded != null) {
      const doneMb = s.current_downloaded / 1e6;
      line += s.current_total
        ? ` — ${formatSize(doneMb)} / ${formatSize(s.current_total / 1e6)}`
        : ` — ${formatSize(doneMb)}`;
    }
    currentEl.textContent = line;
  } else {
    currentEl.textContent = s.message
      || (s.state === 'idle' ? 'Nothing running right now -- select some books and hit Prepare zip, or use Refresh to check for new purchases.' : '');
  }

  // Per-book log (preparing / its result only).
  const logEl = document.getElementById('progress-log');
  logEl.innerHTML = (s.log || []).map(item => {
    if (item.status === 'skipped') {
      return `<li class="skipped"><span class="icon">!</span><span class="title">${escapeHtml(item.title)}</span><span class="detail">${escapeHtml(item.reason || 'Skipped -- no file available')}</span></li>`;
    }
    if (item.status === 'error') {
      return `<li class="error"><span class="icon">✗</span><span class="title">${escapeHtml(item.title)}</span><span class="detail" title="${escapeHtml(item.detail || '')}">${escapeHtml(item.error || 'Download failed')}</span></li>`;
    }
    return `<li class="done"><span class="icon">✓</span><span class="title">${escapeHtml(item.title)}</span><span class="detail">${item.ext}, ${item.size_mb} MB</span></li>`;
  }).join('');
  logEl.scrollTop = logEl.scrollHeight;

  // Only a finished zip build sets `zip_path` (any new activity clears it),
  // so this is what distinguishes "a zip is ready to save" from a size sweep
  // that also ends at result==="done" with a non-zero count.
  document.getElementById('download-link').style.display =
    s.zip_path && s.done > 0 && (s.result === 'done' || s.result === 'cancelled') ? 'inline-block' : 'none';
  document.getElementById('progress-error').textContent = s.error || '';
}

// -- Polling ---------------------------------------------------------------
// A single loop drives everything: fetch the snapshot, paint sizes + the
// activity card, and keep polling while the backend is busy. Any user action
// that starts an activity just calls startPolling().
let polling = false;

async function poll() {
  let s;
  try {
    const resp = await fetch('/activity');
    s = await resp.json();
  } catch (e) {
    setTimeout(poll, 1000); // transient error -- try again next tick
    return;
  }
  const prev = currentState;
  currentState = s.state;

  // A refresh reloads the library list itself -- once it leaves the
  // refreshing state, re-fetch the (now warm) /library so new titles show,
  // then re-apply any sizes resolved so far.
  if (prev === 'refreshing' && s.state !== 'refreshing') {
    await loadLibrary(false);
  }

  applySizes(s.sizes);
  renderActivity(s);
  updateButtons();

  if (BUSY_STATES.has(s.state)) {
    setTimeout(poll, 1000);
  } else {
    polling = false;
  }
}

function startPolling() {
  if (polling) return;
  polling = true;
  poll();
}

// -- Actions: each just tells the backend to start an activity, then polls --
async function startActivity(url, body) {
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json();
    startPolling();
    return data;
  } catch (e) {
    startPolling();
    return { ok: false };
  }
}

document.getElementById('search-box').addEventListener('input', (e) => {
  state.filter = e.target.value;
  renderList();
});

document.getElementById('refresh-library').addEventListener('click', () => {
  if (BUSY_STATES.has(currentState)) return;
  // Optimistically reflect "busy" so a second click in the network window
  // can't fire a second refresh; the next poll replaces this with the truth.
  currentState = 'refreshing';
  updateButtons();
  startActivity('/activity/refresh', { selected: Array.from(state.selected) });
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
  if (state.selected.size === 0 || BUSY_STATES.has(currentState)) return;
  currentState = 'preparing';
  updateButtons();
  const data = await startActivity('/activity/prepare', {
    art_ids: Array.from(state.selected),
    ebook_format: document.getElementById('ebook-format').value,
    audiobook_format: document.getElementById('audiobook-format').value,
  });
  if (data && data.ok === false) {
    alert('Could not start preparing the zip: ' + (data.error || 'unknown error'));
    return;
  }
  document.getElementById('progress-section').scrollIntoView({ behavior: 'smooth' });
});

document.getElementById('cancel-download').addEventListener('click', () => {
  if (currentState !== 'checking' && currentState !== 'preparing') return;
  // Optimistically show "Stopping…" so the click feels responsive even
  // though cancellation only takes effect between books/size fetches (see
  // litres_web/activity.py); the next poll confirms the real state.
  currentState = 'stopping';
  updateButtons();
  fetch('/activity/cancel', { method: 'POST' });
  startPolling();
});

(async function init() {
  await loadLibrary();
  let s;
  try {
    s = await (await fetch('/activity')).json();
  } catch (e) {
    s = { state: 'idle', result: null, sizes: {}, log: [] };
  }
  currentState = s.state;
  applySizes(s.sizes);
  renderActivity(s);
  updateButtons();

  if (BUSY_STATES.has(s.state)) {
    // An activity (e.g. a zip build) survived a page reload -- just attach
    // to it; don't kick off a competing size sweep.
    startPolling();
  } else {
    // Idle: start the background size sweep, then poll it to completion.
    startActivity('/activity/check', { selected: Array.from(state.selected) });
  }
})();
