const state = { books: [], selected: new Set(), filter: '', typeFilter: 'all', sortBy: 'title-asc' };

// -- Activity state machine ------------------------------------------------
// Checking sizes, downloading, and stopping a download all need the one
// shared progress card (#progress-section) and shouldn't run at the same
// time -- letting them overlap would mean the bar/badge jumping between
// unrelated meanings mid-operation. STOPPING is its own state (not a
// separate "cancelRequested" flag bolted onto DOWNLOADING) because it has
// its own button visibility/label rules, same as any other state here.
// Modeling this as an explicit state machine -- rather than ad-hoc
// booleans -- means a future activity (e.g. an export step) is just one
// more STATE value and one more render*() function, not a new set of
// enable/disable rules scattered across every button.
const STATE = { IDLE: 'idle', CHECKING: 'checking', DOWNLOADING: 'downloading', STOPPING: 'stopping' };
let activity = STATE.IDLE;

// STOPPING is entered from either CHECKING or DOWNLOADING (see the
// cancel-download handler below) -- this remembers which one, so the loop
// that was actually running knows to unwind itself. Stopping a download
// goes through the backend (/download/cancel, checked between books);
// stopping a size-check sweep is purely a frontend loop, so it's this flag
// the while loop in fetchSizesInBackground polls each iteration.
let stopRequested = false;

// Every button's visibility/disabled state is a pure function of `activity`
// (plus, for Download, the selection count) -- recomputed as a whole
// rather than each transition trying to patch just the buttons it thinks
// it affects.
function updateButtons() {
  const busy = activity !== STATE.IDLE;
  document.getElementById('refresh-library').disabled = busy;
  document.getElementById('start-download').disabled = busy || state.selected.size === 0;

  // Always visible (like Refresh/Download), just enabled/disabled --
  // hiding it entirely made it disappear before anyone could react when
  // checking sizes resolves from a warm cache in well under a second.
  const cancelBtn = document.getElementById('cancel-download');
  const stopping = activity === STATE.STOPPING;
  const stoppable = activity === STATE.DOWNLOADING || activity === STATE.CHECKING;
  cancelBtn.disabled = !stoppable;
  cancelBtn.textContent = stopping ? 'Stopping…' : 'Stop';
}

function setActivity(next) {
  activity = next;
  updateButtons();
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

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// A FIFO of book ids still needing a size fetch, processed by the single
// paced worker below. Selecting a book jumps it to the front (see
// prioritizeSize) so checking a box doesn't mean waiting for a sweep
// through however many hundreds of other books come first in the list --
// without that, pacing the sweep (below) to avoid looking like scraping
// effectively meant "selected books may never get a size in practice."
let pendingSizeIds = [];

function prioritizeSize(id) {
  const idx = pendingSizeIds.indexOf(id);
  if (idx > 0) {
    pendingSizeIds.splice(idx, 1);
    pendingSizeIds.unshift(id);
  }
}

// The progress card is always on screen (see index.html) -- these
// render*() functions only ever update its contents, never mount/unmount
// it, so switching between idle/checking/downloading reads as the same
// component updating rather than something popping in and out of the page.
function renderIdle(message) {
  const badge = document.getElementById('progress-badge');
  badge.textContent = 'Idle';
  badge.className = 'badge badge-idle';
  const bar = document.getElementById('progress-bar');
  bar.classList.remove('indeterminate');
  bar.style.width = '0%';
  document.getElementById('progress-count').textContent = '';
  document.getElementById('progress-current').textContent = message;
  document.getElementById('progress-error').textContent = '';
}

function renderRefreshingLibrary() {
  const badge = document.getElementById('progress-badge');
  badge.textContent = 'Refreshing…';
  badge.className = 'badge badge-running';
  const bar = document.getElementById('progress-bar');
  bar.classList.add('indeterminate');
  document.getElementById('progress-count').textContent = '';
  document.getElementById('progress-current').textContent = 'Reloading your library list from litres.ru…';
  document.getElementById('progress-error').textContent = '';
}

function renderChecking(done, total) {
  const badge = document.getElementById('progress-badge');
  badge.textContent = activity === STATE.STOPPING ? 'Stopping…' : 'Checking sizes…';
  badge.className = 'badge badge-running';
  const bar = document.getElementById('progress-bar');
  bar.classList.remove('indeterminate');
  bar.style.width = Math.min(100, (done / total) * 100) + '%';
  document.getElementById('progress-count').textContent = `${done} / ${total} sizes checked`;
  document.getElementById('progress-current').textContent =
    done < total ? 'Cached books resolve instantly; new ones are paced to be gentle on litres.ru.' : '';
  document.getElementById('progress-error').textContent = '';
  document.getElementById('progress-log').innerHTML = '';
  document.getElementById('download-link').style.display = 'none';
}

// The actual size-checking loop, assuming the caller has *already* put
// `activity` into CHECKING (or STOPPING, if Stop was clicked before this
// even started) -- e.g. right when the Refresh button was clicked, not
// only once this function gets around to running. Always leaves `activity`
// back at IDLE, regardless of which entry point (below) called it.
async function checkSizes() {
  if (stopRequested) {
    renderIdle('Stopped.');
    stopRequested = false;
    setActivity(STATE.IDLE);
    return;
  }

  pendingSizeIds = state.books.filter(b => b.size_mb == null).map(b => b.id);
  const total = pendingSizeIds.length;
  if (total === 0) {
    setActivity(STATE.IDLE);
    return; // nothing to check -- leave whatever's currently shown alone
  }

  // Sequential on purpose -- the backend has a single dedicated
  // worker thread (Playwright thread-affinity, see session.py), so
  // "parallel" fetches here would just queue up behind each other
  // anyway. Runs after the list is already visible/interactive.
  //
  // Paced on purpose too: a large library (hundreds of books) means this
  // loop fires one request per book every time the page loads -- with no
  // delay, that's a burst of back-to-back calls that reads a lot like
  // scraping to litres.ru's anti-bot checks. A small gap between requests
  // mirrors the pause iter_library already takes between library pages.
  //
  // But only when it's actually a live call: the backend caches these
  // responses (see app/cache.py) and says so via `cached` in the response
  // -- a cache hit didn't touch litres.ru at all, so there's no reason to
  // slow down for it. That also means a book's size shows up immediately
  // whenever it's already known, not just once its turn in the queue
  // comes up, which matters if someone's choosing what to download based
  // on size.
  let done = 0;
  renderChecking(done, total);
  while (pendingSizeIds.length > 0) {
    if (stopRequested) break; // Stop was clicked -- see the cancel-download handler below
    const id = pendingSizeIds.shift();
    const b = state.books.find(x => x.id === id);
    if (!b || b.size_mb != null) { done++; continue; } // already resolved elsewhere -- no need to delay for it
    let wasLiveFetch = true;
    try {
      const resp = await fetch(`/library/${id}/size`);
      const data = await resp.json();
      if (data.ok) {
        wasLiveFetch = !data.cached;
        b.size_mb = data.size_mb;
        const el = document.getElementById(`size-${id}`);
        if (el && b.size_mb != null) el.textContent = `${b.size_mb} MB`;
        if (state.selected.has(id)) updateSelectedCount();
      }
    } catch (e) {
      // best-effort -- leave this row's size blank on failure
    }
    done++;
    renderChecking(done, total);
    if (wasLiveFetch) await sleep(200);
  }
  const wasStopped = stopRequested;
  renderIdle(wasStopped
    ? `Stopped -- checked ${done} of ${total} sizes.`
    : `Checked sizes for ${done} of ${total} book${total === 1 ? '' : 's'}.`);
  stopRequested = false;
  setActivity(STATE.IDLE);
  // Sizes load lazily and can change size-based sort order -- only
  // worth a full re-render for that sort mode, once all sizes are in.
  if (state.sortBy.startsWith('size')) renderList();
}

// Self-contained entry point for the automatic sweep on page load -- claims
// CHECKING itself, unlike the Refresh button (below) which claims it before
// this even starts, to cover the library-reload network round-trip too.
async function fetchSizesInBackground() {
  if (activity !== STATE.IDLE) return; // e.g. a download owns the shared progress card right now
  setActivity(STATE.CHECKING);
  await checkSizes();
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
      if (cb.checked) { state.selected.add(id); prioritizeSize(id); } else { state.selected.delete(id); }
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

  updateButtons();
}

document.getElementById('search-box').addEventListener('input', (e) => {
  state.filter = e.target.value;
  renderList();
});

document.getElementById('refresh-library').addEventListener('click', async () => {
  if (activity !== STATE.IDLE) return;
  // Claimed here, *before* the network round-trip below -- not once
  // checkSizes() gets around to running -- otherwise activity is still
  // IDLE for as long as loadLibrary takes (several seconds against the
  // real litres.ru API), and a second click in that window would start a
  // second concurrent refresh.
  setActivity(STATE.CHECKING);
  renderRefreshingLibrary();
  await loadLibrary(true);
  await checkSizes();
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
  // Reverse order so the *first* visible book ends up at the very front of
  // the pending-size queue after all these prioritizeSize() calls, not the
  // last one -- still paced one-at-a-time, just reordered.
  visibleBooks().slice().reverse().forEach(b => { state.selected.add(b.id); prioritizeSize(b.id); });
  renderList();
});
document.getElementById('select-none').addEventListener('click', () => {
  visibleBooks().forEach(b => state.selected.delete(b.id));
  renderList();
});

document.getElementById('start-download').addEventListener('click', async () => {
  if (state.selected.size === 0 || activity !== STATE.IDLE) return;
  setActivity(STATE.DOWNLOADING);
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
      alert('Could not start preparing the zip: ' + (data.error || 'unknown error'));
      setActivity(STATE.IDLE);
      return;
    }
    document.getElementById('progress-section').scrollIntoView({ behavior: 'smooth' });
    pollStatus();
  } catch (e) {
    setActivity(STATE.IDLE);
  }
});

document.getElementById('cancel-download').addEventListener('click', async () => {
  if (activity !== STATE.DOWNLOADING && activity !== STATE.CHECKING) return;
  const wasDownloading = activity === STATE.DOWNLOADING;
  stopRequested = true;
  setActivity(STATE.STOPPING);
  if (wasDownloading) {
    // Cancellation only takes effect between books (see download_job.py's
    // module docstring -- an in-flight file transfer can't be interrupted),
    // so without an explicit STOPPING state, clicking Stop looks completely
    // unresponsive for as long as the current book takes.
    await fetch('/download/cancel', { method: 'POST' });
  } else {
    // Checking is a purely frontend loop (see fetchSizesInBackground) --
    // it notices stopRequested on its next iteration (at most one paced
    // step away) and unwinds itself back to STATE.IDLE from there.
    document.getElementById('progress-badge').textContent = 'Stopping…';
  }
});

async function pollStatus() {
  const resp = await fetch('/download/status');
  const s = await resp.json();
  renderProgress(s);
  if (s.status === 'running') {
    setTimeout(pollStatus, 1000);
  } else {
    setActivity(STATE.IDLE);
  }
}

function renderProgress(s) {
  const labels = { idle: 'Idle', running: 'Building zip…', done: 'Done', error: 'Error', cancelled: 'Stopped' };
  const badge = document.getElementById('progress-badge');
  const stopping = activity === STATE.STOPPING;
  badge.textContent = stopping ? 'Stopping…' : (labels[s.status] || s.status);
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

  document.getElementById('progress-current').textContent = s.current_title
    ? `Fetching: ${s.current_title}` + (stopping ? ' -- stopping once this one finishes' : '')
    : '';

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

  document.getElementById('download-link').style.display = (s.status === 'done' || s.status === 'cancelled') && s.done > 0 ? 'inline-block' : 'none';
  document.getElementById('progress-error').textContent = s.error || '';
}

(async function init() {
  await loadLibrary();
  const resp = await fetch('/download/status');
  const s = await resp.json();
  if (s.status === 'running') {
    // A download survived a page reload -- it already owns the shared
    // progress card, so don't also kick off a size-check sweep against it.
    setActivity(STATE.DOWNLOADING);
    renderProgress(s);
    pollStatus();
    return;
  }
  if (s.status === 'done' || s.status === 'cancelled') {
    renderProgress(s);
  }
  fetchSizesInBackground();
})();
