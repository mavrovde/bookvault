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
    CHECKING/PREPARING -> STOPPING    -> IDLE     (cancel the current activity)

Only one activity may be in flight; `refresh`/`check_sizes`/`prepare` all
no-op (return False) if the state isn't IDLE. When an activity finishes it
returns to IDLE and records the outcome in `result` (done | cancelled |
error) plus a human `message`, so the UI can show "what just happened"
while sitting idle. The frontend polls `snapshot()` and renders whatever
state it reports -- it owns no activity logic of its own.

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
from typing import Optional

from bookvault_core import cache, session
from bookvault_core.client import DownloadCancelled, LitresBlocked, LitresClient

logger = logging.getLogger(__name__)

# The five states of the machine. IDLE is also where a *finished* activity
# lands -- its outcome is carried in `result`, not in a distinct state, so
# the UI can show "Done"/"Stopped"/"Error" without a separate terminal
# state per activity.
IDLE = "idle"
REFRESHING = "refreshing"
CHECKING = "checking"
PREPARING = "preparing"
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
    "log": [],              # per-book results of the RUNNING prepare: {"title","status",...}
    "results": [],          # durable copy of the last finished prepare's log, so the
                            # results view (and its failed/skipped filter) survives the
                            # size-check that fires on the next page load. Only a NEW
                            # prepare replaces it -- _begin deliberately leaves it alone.
    "error": None,          # raw-ish error line for the UI when result == "error"
    "sizes": {},            # {art_id: size_mb|None} resolved during a sweep, for the UI to paint rows
    "zip_path": None,       # path to the built zip, for the /download/file route
}


def snapshot() -> dict:
    """A safe copy of the current state for the UI to render. The `log` and
    `sizes` collections are copied so a caller can't mutate the live ones."""
    with _lock:
        return {
            **_state,
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
    web UI renders (id/title/authors/is_audio/cover_url)."""
    books = []
    for art in client.iter_library():
        authors = [p.get("full_name") for p in (art.get("persons") or []) if p.get("role") == "author"]
        cover_url = art.get("cover_url")
        books.append(
            {
                "id": art.get("id"),
                "title": art.get("title") or str(art.get("id")),
                "authors": ", ".join(a for a in authors if a),
                "is_audio": art.get("art_type") == 1,
                "cover_url": f"https://static.litres.ru{cover_url}" if cover_url else None,
            }
        )
    return books


def size_of_files(files: list) -> Optional[float]:
    """MB of the best downloadable file in a listing, or None if there's no
    downloadable file at all."""
    best = LitresClient.pick_best_file(files)
    size = best.get("size") if best else None
    return round(size / 1e6, 1) if size else None


def fetch_size(client: LitresClient, art_id, should_cancel=None) -> tuple[Optional[float], list]:
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
            log=[],
            error=None,
            sizes={},
        )
    # Note: `zip_path` and `results` are intentionally NOT reset here, so a
    # finished build's download link and results view survive the size-check
    # that fires on the next page load. Only a new prepare() replaces them.
    _cancel_event.clear()
    return True


def refresh(client: LitresClient, selected: Optional[list] = None) -> bool:
    """Reload the library listing from litres.ru (REFRESHING), then sweep
    book sizes (CHECKING). Returns False if an activity is already running."""
    if not _begin(REFRESHING, message="Reloading your library list from litres.ru…"):
        return False
    logger.info("Starting library refresh")
    session.submit(_run_refresh, client, selected)
    return True


def check_sizes(client: LitresClient, selected: Optional[list] = None, live: bool = True) -> bool:
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
    art_ids: Optional[set] = None,
    preferred_ext: Optional[str] = None,
    preferred_file_type: Optional[str] = None,
) -> bool:
    """Build a zip of the selected books in the background (PREPARING).
    `art_ids` None/empty means "everything"; a specific set restricts the
    zip to those ids. Returns False if an activity is already running."""
    total = len(art_ids) if art_ids is not None else None
    if not _begin(PREPARING, total=total):
        return False
    # A new build supersedes the previous results view AND the previous zip
    # download link (_begin leaves both untouched so they survive size-checks,
    # so clear them explicitly here for the fresh build).
    with _lock:
        previous_zip = _state["zip_path"]
        _state.update(results=[], zip_path=None)
    # Each build gets its own mkdtemp workdir; once superseded, the previous
    # build's zip (potentially many GB) is unreachable -- delete its whole
    # workdir rather than leaking it until the OS cleans the temp dir.
    if previous_zip:
        shutil.rmtree(Path(previous_zip).parent, ignore_errors=True)
    logger.info(
        "Starting zip build: %s, ebook_format=%s, audiobook_format=%s",
        f"{len(art_ids)} selected book(s)" if art_ids is not None else "entire library",
        preferred_ext,
        preferred_file_type,
    )
    session.submit(_run_prepare, client, art_ids, preferred_ext, preferred_file_type)
    return True


def cancel() -> bool:
    """Ask the running activity to stop before its next book/size fetch.
    Only CHECKING and PREPARING are cancellable. Returns False if there's
    nothing stoppable in progress."""
    with _lock:
        if _state["state"] not in (CHECKING, PREPARING):
            return False
        _state["state"] = STOPPING
    logger.info("Cancellation requested")
    _cancel_event.set()
    return True


# --------------------------------------------------------------------------
# Size sweep (CHECKING). Shared by both `check_sizes` and the tail of
# `refresh`, so the two produce identical progress/pacing behaviour.
# --------------------------------------------------------------------------


def _pending_size_ids(books: list, selected: Optional[list]) -> list:
    """Ids of books still needing a size, selected ones first so checking a
    box doesn't mean waiting behind a whole library's worth of others."""
    ids = [b["id"] for b in books]
    if selected:
        id_set, selected_set = set(ids), set(selected)
        chosen = [i for i in selected if i in id_set]
        rest = [i for i in ids if i not in selected_set]
        return chosen + rest
    return ids


def _sweep_sizes(client: LitresClient, books: list, selected: Optional[list], do_live: bool = True) -> None:
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
            except Exception as exc:
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


def _run_check(client: LitresClient, selected: Optional[list], live: bool = True) -> None:
    try:
        books = cache.get_library() or []
        _sweep_sizes(client, books, selected, do_live=live)
    except Exception as exc:
        logger.exception("Size sweep crashed")
        _update(state=IDLE, result="error", error=_friendly_error(exc), message="")


def _run_refresh(client: LitresClient, selected: Optional[list]) -> None:
    try:
        books = build_books(client)
        cache.set_library(books)
    except Exception as exc:
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


def _run_prepare(
    client: LitresClient,
    art_ids: Optional[set],
    preferred_ext: Optional[str],
    preferred_file_type: Optional[str],
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="litres-"))
    zip_path = workdir / "litres-library.zip"
    # Archive member names already used (lowercased -- macOS/Windows extract
    # onto case-insensitive filesystems), so two books that sanitize to the
    # same title get distinct entries instead of silently overwriting each
    # other on extraction.
    used_names: set = set()
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
                    safe_title = "".join(c for c in title if c.isalnum() or c in " ._-")[:150]
                    # A title of pure punctuation/emoji sanitizes to nothing --
                    # fall back to the id rather than writing ".epub". Then
                    # de-collide: same-titled books (or same title after
                    # sanitizing) must not overwrite each other in the archive.
                    if not safe_title.strip():
                        safe_title = str(art_id)
                    if safe_title.lower() in used_names:
                        safe_title = f"{safe_title} ({art_id})"
                    used_names.add(safe_title.lower())
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
                        on_progress=lambda written, total: _update(
                            current_downloaded=written, current_total=total or best_size
                        ),
                    )
                    elapsed = time.monotonic() - started_at
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
                except Exception as exc:
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
            done, total_logged = _state["done"], len(_state["log"])
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
                zip_path=str(zip_path) if done > 0 else None,
                message="Stopped." if cancelled else "",
                # Preserve this build's per-book outcomes so the results view /
                # failed filter survives the next page load's size-check.
                results=list(_state["log"]),
            )
        if done == 0:
            # Nothing to offer -- don't leave the empty archive's workdir
            # behind (the offered-zip case is cleaned by the NEXT prepare).
            shutil.rmtree(workdir, ignore_errors=True)
        logger.info(
            "Zip build %s: %d/%d book(s) succeeded, zip=%s",
            "cancelled" if cancelled else "finished",
            done, total_logged, zip_path,
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
