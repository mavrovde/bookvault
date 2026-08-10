"""The loose-file mirror: the save folder as a plain copy of the library.

Owns DOWNLOADING, and -- more importantly -- the single answer to **"do I
already have this book?"** (`_is_on_disk`). Three features ask that question:
this module's own run, the badge on each book card (`books_on_disk`), and the
zip build's reuse. All three go through `_is_on_disk`; when they were
implemented separately they drifted silently.

The answer comes from `.bookvault-index.json`, which records what a finished
download actually wrote -- never from the size litres.ru's listing declares,
which does not describe the bytes it serves. See
`library_fs.record_in_mirror_index` for the measurement behind that.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import zipfile
from pathlib import Path

from bookvault_core import cache, session
from bookvault_core.client import DownloadCancelled, LitresClient
from bookvault_core.library_fs import (
    extract_audio_zip,
    file_is_complete,
    read_mirror_index,
    record_in_mirror_index,
)

from . import library, state
from .state import DOWNLOADING, IDLE

logger = logging.getLogger(__name__)


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
    if not state._begin(DOWNLOADING, total=len(art_ids) if art_ids is not None else None,
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
    state._update(bytes_done=0, bytes_total=library._expected_total_bytes(art_ids))
    cancelled = False
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        # Read once: it's the record of what previous runs actually wrote, and
        # the only trustworthy answer to "do I already have this book".
        mirror_index = read_mirror_index(dest_root)
        for art in library._iter_books(client):
            if state._cancel_event.is_set():
                cancelled = True
                break

            art_id = art.get("id")
            if art_ids is not None and art_id not in art_ids:
                continue
            title = art.get("title") or str(art_id)
            state._update(current_title=title, current_downloaded=None, current_total=None)

            try:
                files = cache.get_files(art_id)
                if files is None:
                    files = client.get_files(art_id, should_cancel=state._cancel_event.is_set)
                    cache.set_files(art_id, files)
                best = client.pick_best_file(files, preferred_ext, preferred_file_type)
                if best is None:
                    reason = "No downloadable file for this title on litres.ru (rights-limited or preview-only)."
                    logger.info("Skipping %r (art %s): %s", title, art_id, reason)
                    with state._lock:
                        state._state["log"].append({"title": title, "status": "skipped", "reason": reason})
                    continue

                ext = client.file_extension(best)
                expected = best.get("size") or None
                size_mb = round((expected or 0) / 1e6, 1)
                is_audio = art.get("is_audio")
                if is_audio is None:  # raw art dict vs cached web-shape book
                    is_audio = art.get("art_type") == 1
                safe_title = _safe_book_name(title, art_id, used_names)

                # Where the book could land. Which one it *does* land in is
                # only known once the file is here and we can look at it (a
                # zip_with_mp3 bundle unpacks into a folder; every other
                # format, audio or not, is a single file), so both candidates
                # are computed here and the choice is made after the transfer.
                folder_target = dest_root / safe_title
                single_target = dest_root / f"{safe_title}.{ext}"

                # Already here and intact? Say so and move on -- the whole
                # point of a mirror is that running it twice is cheap.
                if _is_on_disk(dest_root, mirror_index, art_id, safe_title, ext, is_audio):
                    logger.info("Already on disk, skipping %r (art %s)", title, art_id)
                    with state._lock:
                        state._state["done"] += 1
                        state._state["log"].append(
                            {"title": title, "ext": ext, "size_mb": size_mb, "status": "exists"}
                        )
                    continue

                # Something is there but doesn't match (a half-finished
                # transfer, a file truncated by a full disk). Worth telling the
                # user apart from a fresh download: "re-downloaded" explains
                # why a book they thought they had is being fetched again.
                existed_but_wrong = folder_target.exists() or single_target.exists()

                state._update(current_downloaded=0, current_total=expected)
                started_at = time.monotonic()
                # Always stage into a temp file next to the destination: a
                # transfer that dies must not leave a half-written file where
                # the finished one belongs, or the next run would treat the
                # wreckage as the book. Same directory so the rename is atomic
                # rather than a cross-filesystem copy.
                staging = dest_root / f".{safe_title}.{ext}.part"
                # One `finally` covering the transfer *and* everything after
                # it. `download_file` discards its own partial on cancel or
                # error, but this layer chose the staging path, so it owns
                # removing it rather than trusting a collaborator to -- and
                # the steps after the transfer (a bundle that isn't a zip, a
                # disk that fills mid-extract, a read-only target) have no
                # such collaborator at all.
                #
                # This runs in the user's own browsable folder, so a leak here
                # is a hidden dotfile that is never retried and never noticed,
                # and gains a sibling on every failed run.
                try:
                    client.download_file(
                        art_id, best["id"], staging.name, staging,
                        should_cancel=state._cancel_event.is_set,
                        on_progress=lambda written, total, fallback=expected, base=completed_bytes: state._update(
                            current_downloaded=written,
                            current_total=total or fallback,
                            bytes_done=base + written,
                        ),
                    )
                    elapsed = time.monotonic() - started_at
                    completed_bytes += staging.stat().st_size
                    state._update(bytes_done=completed_bytes)

                    # Unpack only what is actually an archive. `is_audio` says
                    # the title is an audiobook, NOT that the file we were
                    # served is a zip of tracks: only `zip_with_mp3` is: the
                    # other audio formats (`mobile_version_mp4` and friends,
                    # a common preference) arrive as one file, exactly like an
                    # ebook. Branching on is_audio made every such audiobook
                    # die on "File is not a zip file". Both older paths --
                    # library_fs.install_book and the zip build -- already ask
                    # zipfile.is_zipfile; this one now agrees with them.
                    if is_audio and zipfile.is_zipfile(staging):
                        # A bundle: unpack into a folder per book so the mirror
                        # holds playable tracks, not archives. Rebuilt from
                        # scratch so a re-download can't leave the last
                        # attempt's tracks behind.
                        target = folder_target
                        if target.exists():
                            shutil.rmtree(target, ignore_errors=True)
                        target.mkdir(parents=True, exist_ok=True)
                        extract_audio_zip(staging, target)
                        # Recorded last, so a crash mid-extract leaves no record
                        # and the folder is correctly seen as incomplete next run.
                        record_in_mirror_index(
                            dest_root, art_id, target.name, 0, tracks=_audio_media_count(target)
                        )
                    else:
                        # A single file, whether ebook or single-file audiobook.
                        target = single_target
                        # A folder left by an earlier bundle-shaped download of
                        # the same book (the user changed format) would other-
                        # wise sit there forever looking like the book.
                        if folder_target.is_dir():
                            shutil.rmtree(folder_target, ignore_errors=True)
                        # os.replace: atomic, and overwrites in place -- which is
                        # exactly what a mismatch should do to a partial file.
                        os.replace(staging, target)
                        # Record the length we actually wrote, not the one the
                        # listing claimed: they differ for almost every book, so
                        # only this makes the next run's check meaningful.
                        record_in_mirror_index(
                            dest_root, art_id, target.name, target.stat().st_size
                        )
                finally:
                    staging.unlink(missing_ok=True)
                logger.info(
                    "Saved %r (art %s): %s, %.1f MB in %.1fs", title, art_id, ext, size_mb, elapsed,
                )
            except DownloadCancelled:
                cancelled = True
                break
            except Exception as exc:  # noqa: BLE001 -- one book failing must not sink the whole run
                logger.warning("Download failed for %r (art %s): %s", title, art_id, exc)
                with state._lock:
                    state._state["log"].append(
                        {
                            "title": title,
                            "status": "error",
                            "error": state._friendly_error(exc),
                            "detail": str(exc)[:300],
                        }
                    )
                continue

            with state._lock:
                state._state["done"] += 1
                state._state["log"].append(
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
        state._update(state=IDLE, result="error", error=state._friendly_error(exc), message="",
                current_title=None, current_downloaded=None, current_total=None)
        return

    with state._lock:
        done, entries = state._state["done"], list(state._state["log"])
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
    state._update(
        state=IDLE,
        result="cancelled" if cancelled else "done",
        message=("Stopped. " + summary) if cancelled else summary + ".",
        results=entries,
        current_title=None,
        current_downloaded=None,
        current_total=None,
        done=done,
    )

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

def _recorded(index: dict, art_id):
    """What the index says we last wrote for this book, or an empty dict."""
    return index.get(str(art_id)) or {}

def _is_on_disk(mirror_root: Path | None, index: dict, art_id, safe_title: str, ext: str, is_audio: bool) -> bool:
    """Whether this book is already sitting complete in the mirror.

    Judged against what *we recorded writing* (see
    `library_fs.record_in_mirror_index`), never against litres.ru's listing
    size -- that size does not describe the bytes the site serves, so comparing
    to it would call almost every book incomplete forever.

    **An audiobook is not necessarily a folder.** Only a `zip_with_mp3` bundle
    unpacks into tracks; the other audio formats (`mobile_version_mp4` and
    friends) are a single file, exactly like an ebook. So the shape is decided
    by what we recorded writing -- `tracks` means a folder, `size` means a file
    -- and never by `is_audio`, which says nothing about the delivered form.

    With no record there is nothing to verify against: the user may have put
    the files there themselves. Trust what is present rather than re-fetching a
    library we simply have no history for."""
    if mirror_root is None:
        return False
    rec = _recorded(index, art_id)
    folder = mirror_root / safe_title
    single = mirror_root / f"{safe_title}.{ext}"

    # A record tells us which of the two shapes we wrote, so verify that one.
    if rec.get("tracks") is not None:
        return folder.is_dir() and _audio_media_count(folder) == int(rec["tracks"])
    if rec.get("size"):
        return file_is_complete(single, rec["size"])

    # No record. Accept either shape if it holds anything at all.
    if is_audio and folder.is_dir():
        return _audio_media_count(folder) > 0
    return file_is_complete(single, None)

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
    # Same shape question as _is_on_disk: return the folder only when one is
    # really there, since a single-file audiobook lives at `<title>.<ext>`.
    folder = mirror_root / safe_title
    return folder if folder.is_dir() else mirror_root / f"{safe_title}.{ext}"
