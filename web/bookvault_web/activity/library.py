"""Reading the library and resolving file sizes.

Covers the two read-only activities -- REFRESHING (reload the listing) and
CHECKING (the paced per-book size sweep) -- plus the shared shaping helpers
the routes in `app.py` call directly.

Pacing lives here because it is a property of *this* work: a size sweep is
one request per book, back to back, which is what litres.ru's anti-bot layer
keys on. Cache hits skip the pause entirely -- they never touched the network.
"""
from __future__ import annotations

import logging
import os
import random
import time

from bookvault_core import cache, session
from bookvault_core.client import COVER_BASE, LitresClient

from . import state
from .state import CHECKING, IDLE, REFRESHING

logger = logging.getLogger(__name__)


# Gap between *live* (uncached) per-book size fetches during a sweep. A
# large library means one request per book back-to-back, which reads a lot
# like scraping to litres.ru's anti-bot checks -- a small pause mirrors the
# one iter_library already takes between library pages. Cache hits skip it
# entirely (they never touched litres.ru). Module-level so tests can drop
# it to 0 instead of really sleeping.
PACE_SECONDS = float(os.environ.get("LITRES_SIZE_CHECK_PACE", "0.2"))

# --------------------------------------------------------------------------
# Shared helpers -- also used by web.py's /library and /library/{id}/size
# routes, so the "how we shape a book / compute a size" logic lives in one
# place regardless of whether it's a route or a background sweep asking.
# --------------------------------------------------------------------------


def build_books(client: LitresClient) -> list:
    """Turn the raw litres.ru library listing into the flat book shape the
    web UI renders (id/title/authors/is_audio/cover_url).

    Deliberately builds these five fields directly rather than going through
    `normalize_library_item` and discarding the rest: this runs on the single
    Playwright worker thread for every title on every refresh, and a page load
    waits on it. The genuinely shared pieces -- the cover prefix, the
    author-role filter, the title/id fallback -- are the helpers below, so the
    two surfaces still can't drift on the rules that matter.
    """
    books = []
    for art in client.iter_library():
        books.append(
            {
                "id": art.get("id"),
                "title": LitresClient.title_or_id(art),
                "authors": ", ".join(LitresClient.person_names(art, "author")),
                "is_audio": art.get("art_type") == 1,
                "cover_url": LitresClient._absolute(art.get("cover_url"), COVER_BASE),
            }
        )
    return books

def size_of_files(files: list) -> float | None:
    """MB of the best downloadable file in a listing, or None if there's no
    downloadable file at all."""
    best = LitresClient.pick_best_file(files)
    size = best.get("size") if best else None
    return round(size / 1e6, 1) if size else None

def fetch_size(client: LitresClient, art_id, should_cancel=None) -> tuple[float | None, list]:
    """Live-fetch a book's file listing and return (size_mb, files).
    `should_cancel` lets an anti-bot backoff inside get_files be interrupted
    by a Stop rather than blocking the sweep for the full retry window."""
    files = client.get_files(art_id, should_cancel=should_cancel)
    return size_of_files(files), files

def refresh(client: LitresClient, selected: list | None = None) -> bool:
    """Reload the library listing from litres.ru (REFRESHING), then sweep
    book sizes (CHECKING). Returns False if an activity is already running."""
    if not state._begin(REFRESHING, message="Reloading your library list from litres.ru…"):
        return False
    logger.info("Starting library refresh")
    session.submit(_run_refresh, client, selected)
    return True

def check_sizes(client: LitresClient, selected: list | None = None, live: bool = True) -> bool:
    """Sweep the cached library's book sizes (CHECKING), paced to be gentle
    on litres.ru. `selected` ids, if given, are checked first. When
    `live` is False the sweep is *cache-only*: it resolves sizes already on
    disk and touches litres.ru zero times -- used by the automatic sweep on an
    idle page load, so simply opening/reloading the app never fires a
    library's worth of size requests (the pattern anti-bot checks flag most).
    Live fetching is reserved for the explicit Refresh. Returns False if an
    activity is already running."""
    if not state._begin(CHECKING):
        return False
    logger.info("Starting size sweep (live=%s)", live)
    session.submit(_run_check, client, selected, live)
    return True

def _pending_size_ids(books: list, selected: list | None) -> list:
    """Ids of books still needing a size, selected ones first so checking a
    box doesn't mean waiting behind a whole library's worth of others."""
    ids = [b["id"] for b in books]
    if selected:
        id_set, selected_set = set(ids), set(selected)
        chosen = [i for i in selected if i in id_set]
        rest = [i for i in ids if i not in selected_set]
        return chosen + rest
    return ids

def _sweep_sizes(client: LitresClient, books: list, selected: list | None, do_live: bool = True) -> None:
    """The paced per-book size loop. Assumes the machine is already in
    CHECKING (or will be moved to STOPPING by state.cancel()). Always lands back
    at IDLE with a result of done or cancelled.

    When `do_live` is False the sweep is cache-only: books whose file listing
    isn't already cached are left unresolved instead of being fetched, so the
    sweep makes zero litres.ru requests (see check_sizes)."""
    pending = _pending_size_ids(books, selected)
    total = len(pending)
    state._update(done=0, total=total)
    done = 0
    skipped = 0
    for art_id in pending:
        if state._cancel_event.is_set():
            break
        cached = cache.get_files(art_id)
        if cached is not None:
            size_mb = size_of_files(cached)  # cache hit -- no litres.ru call, no pacing
            live = False
        elif not do_live:
            skipped += 1  # cache-only sweep: don't touch litres.ru for this one
            continue
        else:
            try:
                size_mb, files = fetch_size(client, art_id, should_cancel=state._cancel_event.is_set)
                cache.set_files(art_id, files)
                live = True
            except Exception as exc:  # noqa: BLE001 -- one book's size failing must not abort the whole sweep
                # Best-effort, same as the old frontend loop: leave this row's
                # size blank and move on rather than aborting the whole sweep.
                logger.info("Size fetch failed for art %s: %s", art_id, exc)
                size_mb, live = None, False
        done += 1
        with state._lock:
            state._state["sizes"][art_id] = size_mb
            state._state["done"] = done
            state._state["message"] = (
                "Cached books resolve instantly; new ones are paced to be gentle on litres.ru."
                if done < total
                else ""
            )
        if live and not state._cancel_event.is_set():
            # Jittered gap between live fetches -- a fixed interval is itself a
            # scripted-traffic tell; randomizing it mirrors human-ish pacing.
            time.sleep(random.uniform(PACE_SECONDS, PACE_SECONDS * 2.5))

    cancelled = state._cancel_event.is_set()
    if cancelled:
        message = f"Stopped -- checked {done} of {total} size{'' if total == 1 else 's'}."
    elif skipped:
        message = f"Showing {done} cached size{'' if done == 1 else 's'} -- Refresh to fetch the other {skipped}."
    else:
        message = f"Checked sizes for {done} of {total} book{'' if total == 1 else 's'}."
    state._update(state=IDLE, result="cancelled" if cancelled else "done", message=message)
    logger.info(
        "Size sweep %s: %d resolved, %d skipped (cache-only)",
        "cancelled" if cancelled else "finished", done, skipped,
    )

def _expected_total_bytes(art_ids: set | None) -> int | None:
    """Best estimate of how many bytes the whole build will transfer.

    Sums the same file the download loop will actually pick
    (`pick_best_file`), for every selected book whose listing is already
    cached -- so the figure agrees with the per-book sizes the UI shows rather
    than being a second, differently-derived number.

    Cache-only on purpose: this runs just before a build, and fetching a
    listing per book to firm up a progress denominator is exactly the burst of
    requests the anti-bot layer keys on. Books with no cached listing simply
    contribute nothing, which is why the total is an estimate the UI marks as
    approximate. Returns None when nothing at all is known."""
    books = cache.get_library() or cache.get_library_stale() or []
    total = 0
    known = False
    for book in books:
        art_id = book.get("id")
        if art_ids is not None and art_id not in art_ids:
            continue
        files = cache.get_files(art_id)
        if not files:
            continue
        best = LitresClient.pick_best_file(files)
        size = best.get("size") if best else None
        if size:
            total += int(size)
            known = True
    return total if known else None

def _run_check(client: LitresClient, selected: list | None, live: bool = True) -> None:
    try:
        # Fall back to the *stale* listing, not to nothing. This list is only
        # used to enumerate which art_ids to look a size up for, and the two
        # caches expire on very different clocks: the library listing after 15
        # minutes, a book's file listing after 7 days. So a page opened more
        # than 15 minutes after the last refresh swept an empty list and
        # resolved zero sizes -- reporting "0 of 0" and leaving the UI saying
        # the sizes were unknown while all of them sat fresh on disk.
        #
        # Same reasoning as /library's stale fallback in app.py: a slightly
        # out-of-date set of ids costs at most a brand-new purchase missing
        # its size until the next Refresh, which is far better than every
        # size in the library disappearing on a reload.
        books = cache.get_library() or cache.get_library_stale() or []
        _sweep_sizes(client, books, selected, do_live=live)
    except Exception as exc:
        logger.exception("Size sweep crashed")
        state._update(state=IDLE, result="error", error=state._friendly_error(exc), message="")

def _run_refresh(client: LitresClient, selected: list | None) -> None:
    try:
        books = build_books(client)
        cache.set_library(books)
    except Exception as exc:  # noqa: BLE001 -- a refresh failing must leave the machine IDLE, not wedged
        # A transient blip / anti-bot block / stale client after a
        # login-logout race shouldn't crash the machine -- surface a clean,
        # retryable message and go back to idle.
        logger.warning("Library refresh failed: %s", exc)
        state._update(state=IDLE, result="error", error=state._friendly_error(exc), message="")
        return
    if state._cancel_event.is_set():
        state._update(state=IDLE, result="cancelled", message="Stopped.")
        return
    # Roll straight into a size sweep of the freshly reloaded list, same as
    # the old "refresh, then check sizes" sequence the frontend used to run.
    state._update(state=CHECKING)
    _sweep_sizes(client, books, selected)

def _iter_books(client: LitresClient):
    """Prefer the cached library listing over a fresh full re-sweep -- the
    browser typically fetched it moments ago, and re-fetching just to start
    a download would mean two full sweeps back-to-back. Only id/title are
    used below, and the cached (web) shape carries both under the same keys
    as the raw iter_library() art dicts."""
    cached = cache.get_library()
    if cached is not None:
        return cached
    return list(client.iter_library())
