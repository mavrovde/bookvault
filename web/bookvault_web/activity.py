"""The single backend state machine for everything the app can be *doing*.

Historically this logic lived in the browser: app.js owned an `activity`
enum, a `stopRequested` flag, and the entire paced size-check loop, while
the backend only tracked the download job's own status. That split meant
the rules for "what can run at once / which button is live / when to pace a
request" were spread across the frontend and had to be re-derived there.

They belong here instead, for one concrete reason: the backend has exactly
*one* dedicated Playwright worker thread (thread-affinity -- see
session.py), so at most one real activity can run at a time anyway. This
module makes that implicit constraint the explicit contract:

    IDLE -> REFRESHING  -> (CHECKING) -> IDLE     (reload the library list)
    IDLE -> CHECKING                  -> IDLE     (paced per-book size sweep)
    IDLE -> PREPARING                 -> IDLE     (build the download zip)
    IDLE -> DOWNLOADING               -> IDLE     (loose-file mirror of the selection)
    IDLE -> SYNCING                   -> IDLE     (on-disk ABS library sync)
    CHECKING/PREPARING/DOWNLOADING/SYNCING -> STOPPING -> IDLE (cancel)

Only one activity may be in flight; `refresh`/`check_sizes`/`prepare`/
`download_files`/`start_sync` all no-op (return False) if the state isn't
IDLE. Adding a long-running state here means adding it to `cancel()` too --
omitted, Stop is silently a no-op. When an
activity finishes it returns to IDLE and records the outcome in `result`
(done | cancelled | error) plus a human `message`, so the UI can show
"what just happened" while sitting idle. The frontend polls `snapshot()`
and renders whatever state it reports -- it owns no activity logic of its
own.

Cancellation is cooperative. Between books/size fetches the loop checks the
cancel event, and *within* a download `client.download_file` polls it
between streamed chunks -- so a Stop interrupts an in-flight transfer within
a fraction of a second (the partial file is discarded), rather than having
to wait for a possibly ~2GB audiobook to finish. `client.download_file`
still uses a bounded timeout as a backstop for a transfer that stalls
without delivering any bytes to interrupt on.
"""
from __future__ import annotations

import logging
import os
import random
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from bookvault_core import cache, session
from bookvault_core.client import (
    COVER_BASE,
    DownloadCancelled,
    LitresBlocked,
    LitresClient,
)
from bookvault_core.library_fs import (
    extract_audio_zip,
    file_is_complete,
    library_root_from_env,
    read_mirror_index,
    record_in_mirror_index,
)
from bookvault_core.library_sync import sync_library

logger = logging.getLogger(__name__)

# The five states of the machine. IDLE is also where a *finished* activity
# lands -- its outcome is carried in `result`, not in a distinct state, so
# the UI can show "Done"/"Stopped"/"Error" without a separate terminal
# state per activity.
IDLE = "idle"
REFRESHING = "refreshing"
CHECKING = "checking"
PREPARING = "preparing"
# Downloading the selected books into the save folder as loose files -- the
# same transfers as PREPARING, without the zip. Distinct from SYNCING, which
# builds the Audiobookshelf-shaped tree under LITRES_LIBRARY_DIR.
DOWNLOADING = "downloading"
SYNCING = "syncing"
STOPPING = "stopping"

# Gap between *live* (uncached) per-book size fetches during a sweep. A
# large library means one request per book back-to-back, which reads a lot
# like scraping to litres.ru's anti-bot checks -- a small pause mirrors the
# one iter_library already takes between library pages. Cache hits skip it
# entirely (they never touched litres.ru). Module-level so tests can drop
# it to 0 instead of really sleeping.
PACE_SECONDS = float(os.environ.get("LITRES_SIZE_CHECK_PACE", "0.2"))

_lock = threading.Lock()
_cancel_event = threading.Event()
_state = {
    "state": IDLE,          # idle | refreshing | checking | preparing | stopping
    "result": None,         # None | done | cancelled | error -- outcome of the last finished activity
    "message": "",          # human-friendly line describing what's happening / what just happened
    "current_title": None,  # book currently being fetched (PREPARING only)
    "current_downloaded": None,  # bytes downloaded for the current file (PREPARING only)
    "current_total": None,       # total bytes of the current file when known (PREPARING only)
    "done": 0,              # progress counter (CHECKING: sizes resolved; PREPARING: books zipped)
    "total": None,          # progress denominator when known
    # Whole-build byte progress (PREPARING only). `done` counts books, which
    # says nothing about how much is left when one audiobook outweighs fifty
    # ebooks -- these answer "how far through the download am I" in the unit
    # the user actually cares about. `bytes_total` is an *estimate*: it sums
    # the best file for every selected book whose listing is cached, so a book
    # whose size was never resolved contributes nothing and the real total may
    # come in higher. The UI marks it approximate rather than pretending.
    "bytes_done": 0,        # bytes transferred so far across the whole build
    "bytes_total": None,    # expected total bytes, or None when nothing is known
    "log": [],              # per-book results of the RUNNING prepare: {"title","status",...}
    "results": [],          # durable copy of the last finished prepare's log, so the
                            # results view (and its failed/skipped filter) survives the
                            # size-check that fires on the next page load. Only a NEW
                            # prepare replaces it -- _begin deliberately leaves it alone.
    "error": None,          # raw-ish error line for the UI when result == "error"
    "sizes": {},            # {art_id: size_mb|None} resolved during a sweep, for the UI to paint rows
    "zip_path": None,       # path to the built zip, for the /download/file route
    "saved_path": None,     # where the archive was auto-saved (the configured download
                            # folder), or None when it stayed in its temp workdir. Durable
                            # alongside zip_path so the "Saved to ..." line survives a reload.
    "workdir": None,        # temp dir of the last build, kept ONLY so the next prepare() can
                            # delete it. Never derive this from zip_path: once the archive is
                            # moved into the user's folder, its parent is the USER's folder.
}


def snapshot() -> dict:
    """A safe copy of the current state for the UI to render. The `log` and
    `sizes` collections are copied so a caller can't mutate the live ones;
    `workdir` is internal bookkeeping and is not part of the wire shape."""
    with _lock:
        return {
            **{k: v for k, v in _state.items() if k != "workdir"},
            "log": list(_state["log"]),
            "results": list(_state["results"]),
            "sizes": dict(_state["sizes"]),
        }


def _update(**changes) -> None:
    with _lock:
        _state.update(changes)


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


# --------------------------------------------------------------------------
# Activity entry points. Each claims the machine (IDLE -> its state) under
# the lock, then hands the real work to the one dedicated Playwright thread
# via session.submit so the HTTP request returns immediately.
# --------------------------------------------------------------------------


def _begin(state: str, *, total=None, message="") -> bool:
    """Claim the machine for a new activity. Returns False (a no-op for the
    caller) if something is already running."""
    with _lock:
        if _state["state"] != IDLE:
            logger.info("%s requested while %s is in progress -- ignored", state, _state["state"])
            return False
        _state.update(
            state=state,
            result=None,
            message=message,
            current_title=None,
            current_downloaded=None,
            current_total=None,
            done=0,
            total=total,
            bytes_done=0,
            bytes_total=None,
            log=[],
            error=None,
        )
    # Note: `zip_path`, `saved_path`, `results` and `sizes` are intentionally
    # NOT reset here, so a finished build's download link, "Saved to ..." line
    # and results view survive the size-check that fires on the next page load.
    # Only a new prepare() replaces them.
    #
    # `sizes` is on this side of the line too: it is *derived from the 7-day
    # file-listing cache*, not progress for one activity. Wiping it meant that
    # starting anything -- a download, a refresh, even the automatic check on
    # page load -- blanked every size already on screen, and only a completed
    # sweep put them back. Sizes a sweep re-resolves overwrite these entries
    # anyway, so keeping them costs nothing and stops an operation from
    # destroying information the app already had. Cleared on logout
    # (forget_sizes) so one account's sizes can never paint onto another's.
    _cancel_event.clear()
    return True


def refresh(client: LitresClient, selected: list | None = None) -> bool:
    """Reload the library listing from litres.ru (REFRESHING), then sweep
    book sizes (CHECKING). Returns False if an activity is already running."""
    if not _begin(REFRESHING, message="Reloading your library list from litres.ru…"):
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
    if not _begin(CHECKING):
        return False
    logger.info("Starting size sweep (live=%s)", live)
    session.submit(_run_check, client, selected, live)
    return True


def prepare(
    client: LitresClient,
    art_ids: set | None = None,
    preferred_ext: str | None = None,
    preferred_file_type: str | None = None,
    dest_dir: Path | None = None,
    mirror_root: Path | None = None,
) -> bool:
    """Build a zip of the selected books in the background (PREPARING).

    `mirror_root`, when given, is the loose-file folder to reuse from: a book
    already sitting there complete is packed straight into the archive instead
    of being downloaded again. Requests to litres.ru are the scarce resource
    here, so this is the cheapest possible saving.

    `art_ids` None/empty means "everything"; a specific set restricts the
    zip to those ids. `dest_dir`, when given, is the folder a *successful*
    archive is moved into (see prefs.resolve_download_dir); None leaves it in
    its temp workdir, reachable only via /download/file. Returns False if an
    activity is already running."""
    total = len(art_ids) if art_ids is not None else None
    if not _begin(PREPARING, total=total):
        return False
    # A new build supersedes the previous results view AND the previous zip
    # download link (_begin leaves both untouched so they survive size-checks,
    # so clear them explicitly here for the fresh build).
    with _lock:
        previous_workdir = _state["workdir"]
        _state.update(results=[], zip_path=None, saved_path=None, workdir=None)
    # Each build gets its own mkdtemp workdir; once superseded, whatever is
    # left in the previous one (potentially a many-GB zip) is unreachable --
    # delete it rather than leaking it until the OS cleans the temp dir.
    # Deliberately the recorded workdir, NOT Path(previous_zip).parent: an
    # auto-saved archive lives in the user's own folder, and rmtree-ing its
    # parent would delete that folder and everything else in it.
    if previous_workdir:
        shutil.rmtree(previous_workdir, ignore_errors=True)
    logger.info(
        "Starting zip build: %s, ebook_format=%s, audiobook_format=%s, dest=%s",
        f"{len(art_ids)} selected book(s)" if art_ids is not None else "entire library",
        preferred_ext,
        preferred_file_type,
        dest_dir or "(temp only)",
    )
    session.submit(_run_prepare, client, art_ids, preferred_ext, preferred_file_type, dest_dir, mirror_root)
    return True


def start_sync(
    client: LitresClient,
    *,
    audio_only: bool = True,
    preferred_ext: str | None = None,
    preferred_file_type: str | None = None,
    art_ids: set | None = None,
) -> bool:
    """Sync purchased titles into LITRES_LIBRARY_DIR (SYNCING).

    Returns False if library dir is unset or another activity is running.
    """
    root = library_root_from_env()
    if root is None:
        logger.info("start_sync ignored -- LITRES_LIBRARY_DIR is not set")
        return False
    if not _begin(SYNCING, message="Syncing library to disk…"):
        return False
    logger.info(
        "Starting on-disk library sync into %s (audio_only=%s)",
        root,
        audio_only,
    )
    session.submit(
        _run_sync,
        client,
        root,
        audio_only,
        preferred_ext,
        preferred_file_type,
        art_ids,
    )
    return True


def _run_sync(
    client: LitresClient,
    root,
    audio_only: bool,
    preferred_ext: str | None,
    preferred_file_type: str | None,
    art_ids: set | None,
) -> None:
    try:
        def on_progress(title, done, total):
            _update(
                current_title=title or None,
                done=done,
                total=total if total else None,
                message=f"Syncing “{title}”…" if title else "Finishing library sync…",
            )

        summary = sync_library(
            client,
            root,
            audio_only=audio_only,
            preferred_ext=preferred_ext,
            preferred_file_type=preferred_file_type,
            should_cancel=_cancel_event.is_set,
            on_progress=on_progress,
            art_ids=art_ids,
        )
        cancelled = bool(summary.get("cancelled")) or _cancel_event.is_set()
        done = summary.get("done", 0)
        skipped = summary.get("skipped", 0)
        failed = summary.get("failed", 0)
        if cancelled:
            message = (
                f"Stopped library sync — saved {done}, skipped {skipped}, "
                f"failed {failed}."
            )
        else:
            message = (
                f"Library sync finished — saved {done}, skipped {skipped}, "
                f"failed {failed}."
            )
        with _lock:
            _state.update(
                state=IDLE,
                result="cancelled" if cancelled else "done",
                message=message,
                current_title=None,
                current_downloaded=None,
                current_total=None,
                log=list(summary.get("log") or []),
                results=list(summary.get("log") or []),
                done=done,
                total=summary.get("total"),
            )
        logger.info(
            "Library sync %s: done=%s skipped=%s failed=%s root=%s",
            "cancelled" if cancelled else "finished",
            done,
            skipped,
            failed,
            root,
        )
    except Exception as exc:
        logger.exception("Library sync crashed")
        _update(
            state=IDLE,
            result="error",
            error=_friendly_error(exc),
            current_title=None,
            current_downloaded=None,
            current_total=None,
            message="",
        )


def cancel() -> bool:
    """Ask the running activity to stop before its next book/size fetch.
    CHECKING, PREPARING, DOWNLOADING and SYNCING are cancellable -- every state
    that loops over books and can therefore run for hours. Returns False if
    there's nothing stoppable in progress.

    Adding a long-running state means adding it here too: omitted, Stop is
    silently a no-op and the only way out is killing the server."""
    with _lock:
        if _state["state"] not in (CHECKING, PREPARING, DOWNLOADING, SYNCING):
            return False
        _state["state"] = STOPPING
    logger.info("Cancellation requested")
    _cancel_event.set()
    return True


# --------------------------------------------------------------------------
# Size sweep (CHECKING). Shared by both `check_sizes` and the tail of
# `refresh`, so the two produce identical progress/pacing behaviour.
# --------------------------------------------------------------------------


def forget_sizes() -> None:
    """Drop the resolved sizes held in memory.

    They now survive `_begin` (see the note there), which is right within one
    account and wrong across two: the entries are keyed by art_id, so without
    this a previous account's sizes could paint onto the next one's rows. Called
    on logout, alongside the on-disk `cache.clear()` that does the same job for
    the file listings they were derived from."""
    with _lock:
        _state["sizes"] = {}


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
    CHECKING (or will be moved to STOPPING by cancel()). Always lands back
    at IDLE with a result of done or cancelled.

    When `do_live` is False the sweep is cache-only: books whose file listing
    isn't already cached are left unresolved instead of being fetched, so the
    sweep makes zero litres.ru requests (see check_sizes)."""
    pending = _pending_size_ids(books, selected)
    total = len(pending)
    _update(done=0, total=total)
    done = 0
    skipped = 0
    for art_id in pending:
        if _cancel_event.is_set():
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
                size_mb, files = fetch_size(client, art_id, should_cancel=_cancel_event.is_set)
                cache.set_files(art_id, files)
                live = True
            except Exception as exc:  # noqa: BLE001 -- one book's size failing must not abort the whole sweep
                # Best-effort, same as the old frontend loop: leave this row's
                # size blank and move on rather than aborting the whole sweep.
                logger.info("Size fetch failed for art %s: %s", art_id, exc)
                size_mb, live = None, False
        done += 1
        with _lock:
            _state["sizes"][art_id] = size_mb
            _state["done"] = done
            _state["message"] = (
                "Cached books resolve instantly; new ones are paced to be gentle on litres.ru."
                if done < total
                else ""
            )
        if live and not _cancel_event.is_set():
            # Jittered gap between live fetches -- a fixed interval is itself a
            # scripted-traffic tell; randomizing it mirrors human-ish pacing.
            time.sleep(random.uniform(PACE_SECONDS, PACE_SECONDS * 2.5))

    cancelled = _cancel_event.is_set()
    if cancelled:
        message = f"Stopped -- checked {done} of {total} size{'' if total == 1 else 's'}."
    elif skipped:
        message = f"Showing {done} cached size{'' if done == 1 else 's'} -- Refresh to fetch the other {skipped}."
    else:
        message = f"Checked sizes for {done} of {total} book{'' if total == 1 else 's'}."
    _update(state=IDLE, result="cancelled" if cancelled else "done", message=message)
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


def copy_archive_to(dest_dir: Path) -> Path:
    """Put an *extra* copy of the finished archive in `dest_dir`.

    Deliberately a copy, not a move: the archive stays where the build already
    auto-saved it (the configured save folder). This is the "…and also put one
    over there" case -- an external drive, a shared folder -- and the original
    must survive it.

    Reads `saved_path` first and falls back to `zip_path`, which matters when
    the auto-save failed: the archive is then still sitting in its temp
    workdir, and that is exactly when the user most wants a copy somewhere
    real. Touches none of the state fields, so the download link and the
    "Saved to …" line keep pointing where they did.

    Raises FileNotFoundError when there's no finished archive, and OSError if
    the copy itself fails."""
    with _lock:
        source = _state["saved_path"] or _state["zip_path"]
    if not source or not Path(source).exists():
        raise FileNotFoundError("there is no finished archive to copy")
    source = Path(source)

    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / source.name
    if target.exists() and target.samefile(source):
        # Asked to copy it into the folder it already lives in. Copying onto
        # itself would truncate the archive, and " (2)" of the same file is
        # just litter -- so treat it as already done.
        logger.info("Copy destination is the archive's own folder -- nothing to do")
        return source
    # Same disambiguation as _save_archive: never silently overwrite an
    # archive the user still wants.
    stem, suffix, n = source.stem, source.suffix, 2
    while target.exists():
        target = dest_dir / f"{stem} ({n}){suffix}"
        n += 1
    # copy2 preserves the timestamps, so the copy still reads as "built then".
    shutil.copy2(source, target)
    logger.info("Copied the archive to a second location (%s)", target.name)
    return target


def _audio_media_count(book_dir: Path) -> int:
    """Track files in an unpacked audiobook folder, ignoring bookkeeping.

    An audiobook arrives as a zip and is stored *unpacked*, so no single file
    on disk has a length to check -- a folder that merely exists proves nothing
    about an extract that died on track 3 of 40. Counting the tracks and
    comparing against the count recorded when the extract finished (see
    `library_fs.record_in_mirror_index`) is the equivalent evidence."""
    return sum(1 for p in book_dir.iterdir() if p.is_file() and not p.name.startswith("."))


# Memoised result of the last books_on_disk() scan: (root, expires_at, ids).
# The scan is cheap per book but runs on a route the browser polls once a
# second, over a whole library, taking the cache lock for each book -- while a
# running download holds that same lock to rewrite the listing file after every
# transfer. Uncached, the polls queue up, saturate Starlette's threadpool, and
# then *every* request waits for a free thread, including the one that stops
# the download. Recomputing at most every few seconds removes that entirely;
# a badge lagging a moment behind is invisible next to a multi-minute download.
_on_disk_cache: tuple = (None, 0.0, [])
ON_DISK_TTL = 5.0


def books_on_disk(mirror_root: Path | None, books: list | None = None) -> list:
    """Cached wrapper -- see _scan_books_on_disk for what it computes."""
    global _on_disk_cache
    root, expires_at, ids = _on_disk_cache
    now = time.monotonic()
    if root == mirror_root and now < expires_at:
        return ids
    ids = _scan_books_on_disk(mirror_root, books)
    _on_disk_cache = (mirror_root, now + ON_DISK_TTL, ids)
    return ids


def forget_books_on_disk() -> None:
    """Drop the memoised scan so the next poll re-reads the folder. Called when
    a run that writes files finishes, so badges appear promptly rather than
    after the TTL."""
    global _on_disk_cache
    _on_disk_cache = (None, 0.0, [])


def _scan_books_on_disk(mirror_root: Path | None, books: list | None = None) -> list:
    """art_ids that already sit complete in the loose-file mirror.

    Drives the badge on each book card, so "I already have this" is visible
    before starting anything rather than only in a run's log. Judged by the
    same `_is_on_disk` the download and the zip reuse consult, so the badge
    cannot promise something a run then contradicts.

    Cache-only and cheap: it reads the file listings already on disk and stats
    one path per book (microseconds each), and makes zero litres.ru requests.
    A book whose listing was never cached simply doesn't get a badge."""
    if mirror_root is None or not mirror_root.is_dir():
        return []
    if books is None:
        books = cache.get_library() or cache.get_library_stale() or []
    index = read_mirror_index(mirror_root)
    used_names: set = set()
    present = []
    for book in books:
        art_id = book.get("id")
        title = book.get("title") or str(art_id)
        # Names are assigned in listing order, exactly as a run would assign
        # them, so a de-collided "Title (123)" is looked for under that name.
        safe_title = _safe_book_name(title, art_id, used_names)
        files = cache.get_files(art_id)
        if not files:
            continue
        best = LitresClient.pick_best_file(files)
        if best is None:
            continue
        is_audio = book.get("is_audio")
        if is_audio is None:
            is_audio = book.get("art_type") == 1
        if _is_on_disk(mirror_root, index, art_id, safe_title,
                       LitresClient.file_extension(best), is_audio):
            present.append(art_id)
    return present


def download_files(
    client: LitresClient,
    art_ids: set | None = None,
    preferred_ext: str | None = None,
    preferred_file_type: str | None = None,
    *,
    dest_root: Path,
) -> bool:
    """Download the selected books into `dest_root` as loose files
    (DOWNLOADING). Returns False if another activity is running."""
    if not _begin(DOWNLOADING, total=len(art_ids) if art_ids is not None else None,
                  message="Downloading your books into the save folder…"):
        return False
    logger.info("Starting a loose-file download into %s", dest_root)
    session.submit(_run_download_files, client, art_ids, preferred_ext, preferred_file_type, dest_root)
    return True


def _run_download_files(
    client: LitresClient,
    art_ids: set | None,
    preferred_ext: str | None,
    preferred_file_type: str | None,
    dest_root: Path,
) -> None:
    """The loose-file mirror. Same transfers, pacing and cancellation as the
    zip build -- the difference is only where each file lands, and that a book
    already sitting complete on disk is skipped rather than fetched again."""
    used_names: set = set()
    completed_bytes = 0
    _update(bytes_done=0, bytes_total=_expected_total_bytes(art_ids))
    cancelled = False
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        # Read once: it's the record of what previous runs actually wrote, and
        # the only trustworthy answer to "do I already have this book".
        mirror_index = read_mirror_index(dest_root)
        for art in _iter_books(client):
            if _cancel_event.is_set():
                cancelled = True
                break

            art_id = art.get("id")
            if art_ids is not None and art_id not in art_ids:
                continue
            title = art.get("title") or str(art_id)
            _update(current_title=title, current_downloaded=None, current_total=None)

            try:
                files = cache.get_files(art_id)
                if files is None:
                    files = client.get_files(art_id, should_cancel=_cancel_event.is_set)
                    cache.set_files(art_id, files)
                best = client.pick_best_file(files, preferred_ext, preferred_file_type)
                if best is None:
                    reason = "No downloadable file for this title on litres.ru (rights-limited or preview-only)."
                    logger.info("Skipping %r (art %s): %s", title, art_id, reason)
                    with _lock:
                        _state["log"].append({"title": title, "status": "skipped", "reason": reason})
                    continue

                ext = client.file_extension(best)
                expected = best.get("size") or None
                size_mb = round((expected or 0) / 1e6, 1)
                is_audio = art.get("is_audio")
                if is_audio is None:  # raw art dict vs cached web-shape book
                    is_audio = art.get("art_type") == 1
                safe_title = _safe_book_name(title, art_id, used_names)

                # Already here and intact? Say so and move on -- the whole
                # point of a mirror is that running it twice is cheap.
                target = dest_root / (safe_title if is_audio else f"{safe_title}.{ext}")
                if _is_on_disk(dest_root, mirror_index, art_id, safe_title, ext, is_audio):
                    logger.info("Already on disk, skipping %r (art %s)", title, art_id)
                    with _lock:
                        _state["done"] += 1
                        _state["log"].append(
                            {"title": title, "ext": ext, "size_mb": size_mb, "status": "exists"}
                        )
                    continue

                # Something is there but doesn't match (a half-finished
                # transfer, a file truncated by a full disk). Worth telling the
                # user apart from a fresh download: "re-downloaded" explains
                # why a book they thought they had is being fetched again.
                existed_but_wrong = target.exists()

                _update(current_downloaded=0, current_total=expected)
                started_at = time.monotonic()
                # Always stage into a temp file next to the destination: a
                # transfer that dies must not leave a half-written file where
                # the finished one belongs, or the next run would treat the
                # wreckage as the book. Same directory so the rename is atomic
                # rather than a cross-filesystem copy.
                staging = dest_root / f".{safe_title}.{ext}.part"
                client.download_file(
                    art_id, best["id"], staging.name, staging,
                    should_cancel=_cancel_event.is_set,
                    on_progress=lambda written, total, fallback=expected, base=completed_bytes: _update(
                        current_downloaded=written,
                        current_total=total or fallback,
                        bytes_done=base + written,
                    ),
                )
                elapsed = time.monotonic() - started_at
                completed_bytes += staging.stat().st_size
                _update(bytes_done=completed_bytes)

                if is_audio:
                    # An audiobook arrives as a zip of tracks; unpack it into a
                    # folder per book so the mirror holds playable files, not
                    # archives. Rebuilt from scratch so a re-download after a
                    # size mismatch can't leave last attempt's tracks behind.
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    target.mkdir(parents=True, exist_ok=True)
                    extract_audio_zip(staging, target)
                    staging.unlink(missing_ok=True)
                    # Recorded last, so a crash mid-extract leaves no record and
                    # the folder is correctly seen as incomplete next run.
                    record_in_mirror_index(
                        dest_root, art_id, target.name, 0, tracks=_audio_media_count(target)
                    )
                else:
                    # os.replace: atomic, and overwrites in place -- which is
                    # exactly what a size mismatch should do to a partial file.
                    os.replace(staging, target)
                    # Record the length we actually wrote, not the one the
                    # listing claimed: they differ for almost every book, so
                    # only this makes the next run's check meaningful.
                    record_in_mirror_index(dest_root, art_id, target.name, target.stat().st_size)
                logger.info(
                    "Saved %r (art %s): %s, %.1f MB in %.1fs", title, art_id, ext, size_mb, elapsed,
                )
            except DownloadCancelled:
                cancelled = True
                break
            except Exception as exc:  # noqa: BLE001 -- one book failing must not sink the whole run
                logger.warning("Download failed for %r (art %s): %s", title, art_id, exc)
                with _lock:
                    _state["log"].append(
                        {
                            "title": title,
                            "status": "error",
                            "error": _friendly_error(exc),
                            "detail": str(exc)[:300],
                        }
                    )
                continue

            with _lock:
                _state["done"] += 1
                _state["log"].append(
                    {
                        "title": title,
                        "ext": ext,
                        "size_mb": size_mb,
                        "status": "replaced" if existed_but_wrong else "done",
                    }
                )
    except Exception as exc:
        # A run-level failure (the listing sweep dying, an unwritable folder)
        # must leave the machine IDLE rather than wedged mid-activity.
        logger.exception("Loose-file download crashed")
        _update(state=IDLE, result="error", error=_friendly_error(exc), message="",
                current_title=None, current_downloaded=None, current_total=None)
        return

    with _lock:
        done, entries = _state["done"], list(_state["log"])
    fresh = sum(1 for e in entries if e.get("status") == "done")
    replaced = sum(1 for e in entries if e.get("status") == "replaced")
    existing = sum(1 for e in entries if e.get("status") == "exists")
    # The folder just changed, so the memoised badge scan is stale.
    forget_books_on_disk()
    summary = f"Downloaded {fresh} book{'' if fresh == 1 else 's'} into {dest_root}"
    if replaced:
        summary += f"; re-downloaded {replaced} that were incomplete"
    if existing:
        summary += f"; {existing} already saved"
    _update(
        state=IDLE,
        result="cancelled" if cancelled else "done",
        message=("Stopped. " + summary) if cancelled else summary + ".",
        results=entries,
        current_title=None,
        current_downloaded=None,
        current_total=None,
        done=done,
    )


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
        _update(state=IDLE, result="error", error=_friendly_error(exc), message="")


def _run_refresh(client: LitresClient, selected: list | None) -> None:
    try:
        books = build_books(client)
        cache.set_library(books)
    except Exception as exc:  # noqa: BLE001 -- a refresh failing must leave the machine IDLE, not wedged
        # A transient blip / anti-bot block / stale client after a
        # login-logout race shouldn't crash the machine -- surface a clean,
        # retryable message and go back to idle.
        logger.warning("Library refresh failed: %s", exc)
        _update(state=IDLE, result="error", error=_friendly_error(exc), message="")
        return
    if _cancel_event.is_set():
        _update(state=IDLE, result="cancelled", message="Stopped.")
        return
    # Roll straight into a size sweep of the freshly reloaded list, same as
    # the old "refresh, then check sizes" sequence the frontend used to run.
    _update(state=CHECKING)
    _sweep_sizes(client, books, selected)


# --------------------------------------------------------------------------
# Zip build (PREPARING). This is the former download_job._run, moved here so
# every activity shares one state machine, lock, and cancel event.
# --------------------------------------------------------------------------


def _safe_book_name(title: str, art_id, used_names: set) -> str:
    """Filesystem/archive-safe base name for one book, de-collided.

    Shared by the zip build and the loose-file download so a book lands under
    the same name in both -- an archive extracted next to a synced folder
    should look identical, not subtly differently named.

    A title of pure punctuation/emoji sanitizes to nothing, so it falls back to
    the id rather than producing a bare ".epub"; two books that sanitize to the
    same string get the id appended so neither overwrites the other."""
    safe = "".join(c for c in title if c.isalnum() or c in " ._-")[:150]
    if not safe.strip():
        safe = str(art_id)
    if safe.lower() in used_names:
        safe = f"{safe} ({art_id})"
    used_names.add(safe.lower())
    return safe


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


def _add_folder_to_zip(zf: zipfile.ZipFile, folder: Path, safe_title: str) -> None:
    """Add an already-extracted audiobook folder to the archive.

    The loose-file mirror stores audiobooks unpacked, so reusing one means
    adding its tracks directly rather than re-zipping them. Members go in
    STORED under a per-book folder -- byte-identical to what the audio branch
    of _add_to_zip produces after unpacking a freshly downloaded bundle, so an
    archive built from the mirror matches one built from downloads."""
    for track in sorted(p for p in folder.iterdir() if p.is_file()):
        if track.name.startswith("."):
            continue  # bookkeeping (index, .part staging), not part of the book
        zf.write(track, arcname=f"{safe_title}/{track.name}", compress_type=zipfile.ZIP_STORED)


def _recorded(index: dict, art_id):
    """What the index says we last wrote for this book, or an empty dict."""
    return index.get(str(art_id)) or {}


def _is_on_disk(mirror_root: Path | None, index: dict, art_id, safe_title: str, ext: str, is_audio: bool) -> bool:
    """Whether this book is already sitting complete in the mirror.

    Judged against what *we recorded writing* (see
    `library_fs.record_in_mirror_index`), never against litres.ru's listing
    size -- that size does not describe the bytes the site serves, so comparing
    to it would call almost every book incomplete forever.

    With no record we cannot verify anything, so a present file is trusted
    rather than re-downloaded."""
    if mirror_root is None:
        return False
    rec = _recorded(index, art_id)
    target = mirror_root / (safe_title if is_audio else f"{safe_title}.{ext}")
    if is_audio:
        if not target.is_dir():
            return False
        tracks = rec.get("tracks")
        # No record, or the folder lost a track since we wrote it.
        return bool(tracks) and _audio_media_count(target) == int(tracks)
    return file_is_complete(target, rec.get("size"))


def _local_copy_for(mirror_root: Path | None, index: dict, art_id, safe_title: str, ext: str,
                    is_audio: bool):
    """A complete copy of this book already in the loose-file mirror, or None.

    Lets "Prepare zip" build from files already on disk instead of downloading
    them again -- the cheapest possible win against the thing that actually
    constrains this app: requests to litres.ru.

    Delegates the judgement to `_is_on_disk` rather than repeating it. The two
    questions ("should the mirror re-fetch this?" and "can the zip reuse it?")
    are the same question about the same file, and when they were implemented
    separately they drifted: the mirror moved to the recorded-size index while
    this one still compared against the catalogue, so reuse silently never
    fired. One definition, one place to change it."""
    if mirror_root is None:
        return None
    if not _is_on_disk(mirror_root, index, art_id, safe_title, ext, is_audio):
        return None
    return mirror_root / (safe_title if is_audio else f"{safe_title}.{ext}")


def _add_to_zip(zf: zipfile.ZipFile, dest: Path, safe_title: str, is_audio: bool) -> None:
    """Add one downloaded book to the archive so macOS Archive Utility can open
    the result, without re-compressing gigabytes of already-compressed audio.

    Archive Utility locates the central directory by scanning for the
    end-of-central-directory signature (PK\\x05\\x06); if a member is itself a
    zip stored uncompressed, that signature appears raw inside the outer
    archive and Archive Utility, seeing several, rejects the file as an
    "unsupported format" (`unzip`/`ditto`, which read the real directory, are
    fine). Three cases:

    - Audiobook bundle (zip_with_mp3 -- a zip of mp3s): unpack it and add each
      track STORED under a per-book folder. No re-compression, and no nested
      zip signature to confuse the parser.
    - Any other member that is *itself* a zip (epub, fb2.zip, fb3, ...): keep
      it as one file but DEFLATE it, which rewrites the bytes so the nested
      signatures no longer appear raw. These are small, so it's cheap.
    - Everything else (m4b, mp3, pdf, txt, mobi): add STORED -- it has no
      nested zip signature, so storing it is both safe and free.
    """
    member_is_zip = zipfile.is_zipfile(dest)
    if is_audio and member_is_zip:
        with zipfile.ZipFile(dest) as inner:
            for info in inner.infolist():
                if info.is_dir():
                    continue
                entry = zipfile.ZipInfo(f"{safe_title}/{PurePosixPath(info.filename).name}")
                entry.compress_type = zipfile.ZIP_STORED
                with inner.open(info) as src, zf.open(entry, "w") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
        return
    if member_is_zip:
        zf.write(dest, arcname=dest.name, compress_type=zipfile.ZIP_DEFLATED, compresslevel=1)
    else:
        zf.write(dest, arcname=dest.name, compress_type=zipfile.ZIP_STORED)


def _save_archive(zip_path: Path, dest_dir: Path) -> Path:
    """Move a finished archive out of its temp workdir into the user's chosen
    folder, under a timestamped name. Timestamped rather than fixed so a new
    build never silently overwrites an archive the user still wants; the
    numeric suffix settles the (unlikely) same-second collision. Returns the
    saved path. Raises OSError if the folder can't be written."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = f"litres-library-{time.strftime('%Y%m%d-%H%M%S')}"
    target = dest_dir / f"{stem}.zip"
    n = 2
    while target.exists():
        target = dest_dir / f"{stem} ({n}).zip"
        n += 1
    # shutil.move, not os.replace: the download folder is very often on a
    # different filesystem than the temp dir (external drive, network share).
    shutil.move(str(zip_path), str(target))
    return target


def _run_prepare(
    client: LitresClient,
    art_ids: set | None,
    preferred_ext: str | None,
    preferred_file_type: str | None,
    dest_dir: Path | None = None,
    mirror_root: Path | None = None,
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="litres-"))
    zip_path = workdir / "litres-library.zip"
    # Archive member names already used (lowercased -- macOS/Windows extract
    # onto case-insensitive filesystems), so two books that sanitize to the
    # same title get distinct entries instead of silently overwriting each
    # other on extraction.
    used_names: set = set()
    # Whole-build byte progress. `completed_bytes` only grows when a file has
    # finished, so a transfer that fails or is cancelled part-way doesn't leave
    # its partial bytes counted against the total; the live figure adds the
    # current file's progress on top.
    completed_bytes = 0
    _update(bytes_done=0, bytes_total=_expected_total_bytes(art_ids))
    # Read once per build: the record of what the mirror actually wrote, and
    # so the only sound basis for reusing a file instead of re-fetching it.
    mirror_index = read_mirror_index(mirror_root) if mirror_root else {}
    try:
        # Default STORED; _add_to_zip picks the right per-member scheme (see
        # its docstring). The goal is an archive macOS Archive Utility can open
        # without re-compressing gigabytes of already-compressed audio.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            cancelled = False
            for art in _iter_books(client):
                if _cancel_event.is_set():
                    cancelled = True
                    break

                art_id = art.get("id")
                if art_ids is not None and art_id not in art_ids:
                    continue
                title = art.get("title") or str(art_id)
                _update(current_title=title, current_downloaded=None, current_total=None)
                logger.info("Downloading %r (art %s)", title, art_id)

                try:
                    files = cache.get_files(art_id)
                    if files is None:
                        files = client.get_files(art_id, should_cancel=_cancel_event.is_set)
                        cache.set_files(art_id, files)
                    best = client.pick_best_file(files, preferred_ext, preferred_file_type)
                    if best is None:
                        reason = "No downloadable file for this title on litres.ru (rights-limited or preview-only)."
                        logger.info("Skipping %r (art %s): %s", title, art_id, reason)
                        with _lock:
                            _state["log"].append({"title": title, "status": "skipped", "reason": reason})
                        continue

                    ext = client.file_extension(best)
                    size_mb = round(best.get("size", 0) / 1e6, 1)
                    is_audio = art.get("is_audio")
                    if is_audio is None:  # raw art dict vs cached web-shape book
                        is_audio = art.get("art_type") == 1
                    safe_title = _safe_book_name(title, art_id, used_names)

                    # Already downloaded as a loose file? Pack that copy
                    # instead of fetching it again. Saves the transfer and,
                    # more importantly, the request -- and the completeness
                    # test is the same one the mirror uses, so a half-written
                    # file is never packed as though it were the book.
                    local = _local_copy_for(mirror_root, mirror_index, art_id, safe_title, ext, is_audio)
                    if local is not None:
                        if is_audio:
                            _add_folder_to_zip(zf, local, safe_title)
                        else:
                            _add_to_zip(zf, local, safe_title, is_audio)
                        logger.info("Packed %r (art %s) from the local folder", title, art_id)
                        with _lock:
                            _state["done"] += 1
                            _state["log"].append(
                                {"title": title, "ext": ext, "size_mb": size_mb, "status": "reused"}
                            )
                        continue

                    dest = workdir / f"{safe_title}.{ext}"
                    # Seed the total from the known file size so the MB readout
                    # shows "0 / N MB" the instant the transfer starts; the
                    # callback prefers the live Content-Length but falls back to
                    # this when the server sends none, so the total never blanks.
                    best_size = best.get("size") or None
                    _update(current_downloaded=0, current_total=best_size)
                    started_at = time.monotonic()
                    client.download_file(
                        art_id, best["id"], dest.name, dest,
                        should_cancel=_cancel_event.is_set,
                        # `best_size` is bound as a default rather than captured
                        # from the enclosing loop: the callback is only ever
                        # invoked during this iteration, but binding makes that
                        # guarantee explicit instead of relying on it.
                        on_progress=lambda written, total, fallback=best_size, base=completed_bytes: _update(
                            current_downloaded=written,
                            current_total=total or fallback,
                            # `base` is bound per-iteration for the same reason
                            # as `fallback`: the whole-build figure is the bytes
                            # already banked plus this file's live progress.
                            bytes_done=base + written,
                        ),
                    )
                    elapsed = time.monotonic() - started_at
                    # Bank this file's real size (not the estimate) now that it
                    # has landed, so the whole-build figure self-corrects when a
                    # book turned out bigger or smaller than its cached listing
                    # said -- and so a later failure can't subtract from it.
                    completed_bytes += dest.stat().st_size
                    _update(bytes_done=completed_bytes)
                    _add_to_zip(zf, dest, safe_title, is_audio)
                    dest.unlink()
                    logger.info(
                        "Downloaded %r (art %s): %s, %.1f MB in %.1fs",
                        title, art_id, ext, size_mb, elapsed,
                    )
                except DownloadCancelled:
                    # Stop was pressed mid-transfer -- download_file already
                    # discarded the partial file. Stop the queue cleanly (this
                    # book is neither "done" nor an error, just not included).
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001 -- one book failing must not sink a multi-hour zip build
                    # One book failing (a stalled/timed-out transfer, an
                    # anti-bot block, ...) shouldn't sink the whole job --
                    # log the raw detail and show a friendly message + reason
                    # to the user, then move on.
                    logger.warning("Download failed for %r (art %s): %s", title, art_id, exc)
                    with _lock:
                        _state["log"].append(
                            {
                                "title": title,
                                "status": "error",
                                "error": _friendly_error(exc),
                                "detail": str(exc)[:300],
                            }
                        )
                    continue

                with _lock:
                    _state["done"] += 1
                    _state["log"].append(
                        {"title": title, "ext": ext, "size_mb": size_mb, "status": "done"}
                    )
        with _lock:
            done = _state["done"]

        # Move the finished archive into the user's folder, if they configured
        # one. Deliberately after the build, never during: a crashed or empty
        # build must not leave a half-written .zip sitting in their folder.
        final_zip, saved_path, save_error = zip_path, None, None
        if done > 0 and dest_dir is not None:
            try:
                final_zip = _save_archive(zip_path, dest_dir)
                saved_path = str(final_zip)
                logger.info("Archive saved to %s", final_zip)
            except OSError as exc:
                # Read-only folder, full disk, unplugged volume... Keep the
                # archive in the workdir so it's still downloadable rather
                # than throwing away a build that may have taken hours.
                logger.warning("Could not save the archive to %s: %s", dest_dir, exc)
                save_error = (
                    f"Couldn't save to {dest_dir} ({exc.strerror or exc}). "
                    "The archive is still available with the button below."
                )

        with _lock:
            total_logged = len(_state["log"])
            _state.update(
                state=IDLE,
                result="cancelled" if cancelled else "done",
                current_title=None,
                current_downloaded=None,
                current_total=None,
                # Offer the zip only if it actually holds something; a build
                # where every book failed/was skipped produces an empty archive
                # not worth downloading. Durable (see _begin) so the link
                # survives a reload's size-check.
                zip_path=str(final_zip) if done > 0 else None,
                saved_path=saved_path,
                # Keep the workdir only while it still holds the archive; once
                # moved out, there's nothing left worth keeping.
                workdir=None if (done == 0 or saved_path) else str(workdir),
                message=" ".join(p for p in ("Stopped." if cancelled else "", save_error or "") if p),
                # Preserve this build's per-book outcomes so the results view /
                # failed filter survives the next page load's size-check.
                results=list(_state["log"]),
            )
        if done == 0 or saved_path:
            # Nothing left in the workdir -- either the build produced no
            # archive worth offering, or the archive has been moved out of it.
            # (The still-in-temp case is cleaned by the NEXT prepare.)
            shutil.rmtree(workdir, ignore_errors=True)
        logger.info(
            "Zip build %s: %d/%d book(s) succeeded, zip=%s",
            "cancelled" if cancelled else "finished",
            done, total_logged, final_zip,
        )
    except Exception as exc:
        logger.exception("Zip build crashed")
        _update(
            state=IDLE, result="error", error=_friendly_error(exc),
            current_title=None, current_downloaded=None, current_total=None, message="",
            results=list(_state["log"]),  # keep whatever finished before the crash
        )
        # A crashed build never offers its zip (zip_path stays None), so its
        # workdir -- with the unfinished archive and any leftovers -- is garbage.
        shutil.rmtree(workdir, ignore_errors=True)


def _friendly_error(exc: Exception) -> str:
    """Translate a raw client exception into a short, actionable message for
    the UI -- the raw text (a truncated HTML challenge page, a Playwright
    timeout repr, ...) is logged in full via `logger` but isn't fit to show
    a non-technical user."""
    text = str(exc)
    lower = text.lower()
    # A LitresBlocked that reaches here survived the client's own retries +
    # cookie re-warm, so it's a genuinely persistent anti-bot/rate-limit block:
    # waiting a few minutes is the only thing that helps (retrying now won't).
    if isinstance(exc, LitresBlocked) or "ddos-guard" in lower:
        return "litres.ru's anti-bot check kept blocking this even after automatic retries -- wait a few minutes, then try again."
    if "(403)" in text:
        # Not an anti-bot block (the client already tried both the purchase and
        # subscription download endpoints and litres refused both) -- retrying
        # won't change the answer, so don't tell the user to.
        return "litres.ru won't serve this title (403) -- it may be subscription-only, region-locked, or preview-only. The other books still downloaded."
    if "(429)" in text:
        return "Rate-limited by litres.ru (429) -- wait a few minutes before retrying."
    if "(401)" in text or "permissionmissing" in lower:
        return "Session looks expired -- try logging out and back in."
    if "timeout" in lower:
        return "Download timed out -- the file may be large or the connection slow."
    if "event loop is closed" in lower or "already stopped" in lower:
        return "Session changed while this was running (e.g. a login/logout) -- refresh the page and retry."
    if "socket hang up" in lower or "econnreset" in lower:
        return "Connection to litres.ru was interrupted -- wait a bit, then retry."
    return f"Download failed: {text[:150]}"
