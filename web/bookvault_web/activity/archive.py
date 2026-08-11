"""The zip build (PREPARING) and what happens to the finished archive.

A build always writes into its own temp workdir and the archive is moved into
the user's folder only after it succeeds, so a crashed or empty build never
leaves a half-written `.zip` in someone's Downloads.

Where a book already sits in the loose-file mirror it is packed from there
rather than fetched again -- requests to litres.ru are the scarce resource,
not bytes. That reuse asks `mirror._is_on_disk`, the same question the mirror
asks itself.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from bookvault_core import cache, session
from bookvault_core.client import DownloadCancelled, LitresClient
from bookvault_core.library_fs import read_mirror_index

from . import library, mirror, state
from .state import IDLE, PREPARING

logger = logging.getLogger(__name__)


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
    if not state._begin(PREPARING, total=total):
        return False
    # `_begin` has already cleared the previous results and the old download
    # link (see PRODUCES_RESULTS there). What remains is this build's own
    # bookkeeping: take the previous workdir so it can be deleted below.
    with state._lock:
        previous_workdir = state._state["workdir"]
        state._state["workdir"] = None
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
    with state._lock:
        source = state._state["saved_path"] or state._state["zip_path"]
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
    # Books packed from the loose-file mirror instead of downloaded. Held back
    # from the live log and merged in at the end -- see the reuse branch below.
    reused_local: list = []
    state._update(bytes_done=0, bytes_total=library._expected_total_bytes(art_ids))
    # Read once per build: the record of what the mirror actually wrote, and
    # so the only sound basis for reusing a file instead of re-fetching it.
    mirror_index = read_mirror_index(mirror_root) if mirror_root else {}
    try:
        # Default STORED; _add_to_zip picks the right per-member scheme (see
        # its docstring). The goal is an archive macOS Archive Utility can open
        # without re-compressing gigabytes of already-compressed audio.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            cancelled = False
            for art in library._iter_books(client):
                if state._cancel_event.is_set():
                    cancelled = True
                    break

                art_id = art.get("id")
                if art_ids is not None and art_id not in art_ids:
                    continue
                title = art.get("title") or str(art_id)
                state._update(current_title=title, current_downloaded=None, current_total=None)
                logger.info("Downloading %r (art %s)", title, art_id)

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
                    size_mb = round(best.get("size", 0) / 1e6, 1)
                    is_audio = art.get("is_audio")
                    if is_audio is None:  # raw art dict vs cached web-shape book
                        is_audio = art.get("art_type") == 1
                    safe_title = mirror._safe_book_name(title, art_id, used_names)

                    # Already downloaded as a loose file? Pack that copy
                    # instead of fetching it again. Saves the transfer and,
                    # more importantly, the request -- and the completeness
                    # test is the same one the mirror uses, so a half-written
                    # file is never packed as though it were the book.
                    local = mirror._local_copy_for(mirror_root, mirror_index, art_id, safe_title, ext, is_audio)
                    if local is not None:
                        # Dispatch on what the local copy *is*, not on
                        # is_audio: only a zip_with_mp3 bundle is stored as a
                        # folder of tracks. Every other format -- including
                        # single-file audiobooks like mobile_version_mp4 -- is
                        # one file and must be packed as one.
                        if local.is_dir():
                            _add_folder_to_zip(zf, local, safe_title)
                        else:
                            _add_to_zip(zf, local, safe_title, is_audio)
                        logger.info("Packed %r (art %s) from the local folder", title, art_id)
                        # Reused bytes are still bytes the build accounted for.
                        # `bytes_total` counted this book, so not crediting it
                        # here would make the readout drift further behind the
                        # more the mirror saves us -- worst in exactly the case
                        # the reuse is designed for. Credited at its listed
                        # size, matching how the denominator counted it.
                        completed_bytes += best.get("size") or 0
                        # Held back from the live log; merged in at the end.
                        reused_local.append(
                            {"title": title, "ext": ext, "size_mb": size_mb, "status": "reused"}
                        )
                        with state._lock:
                            state._state["done"] += 1
                        state._update(bytes_done=completed_bytes)
                        continue

                    dest = workdir / f"{safe_title}.{ext}"
                    # Seed the total from the known file size so the MB readout
                    # shows "0 / N MB" the instant the transfer starts; the
                    # callback prefers the live Content-Length but falls back to
                    # this when the server sends none, so the total never blanks.
                    best_size = best.get("size") or None
                    state._update(current_downloaded=0, current_total=best_size)
                    started_at = time.monotonic()
                    client.download_file(
                        art_id, best["id"], dest.name, dest,
                        should_cancel=state._cancel_event.is_set,
                        # `best_size` is bound as a default rather than captured
                        # from the enclosing loop: the callback is only ever
                        # invoked during this iteration, but binding makes that
                        # guarantee explicit instead of relying on it.
                        on_progress=lambda written, total, fallback=best_size, base=completed_bytes: state._update(
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
                    state._update(bytes_done=completed_bytes)
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
                        {"title": title, "ext": ext, "size_mb": size_mb, "status": "done"}
                    )
        with state._lock:
            done = state._state["done"]

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

        with state._lock:
            # Merge the held-back reuses now the build is over -- same reason
            # as the mirror's skips: they resolve instantly, so streaming them
            # live buried the books actually being fetched.
            state._state["log"].extend(reused_local)
            total_logged = len(state._state["log"])
            state._state.update(
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
                results=list(state._state["log"]),
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
        state._update(
            state=IDLE, result="error", error=state._friendly_error(exc),
            current_title=None, current_downloaded=None, current_total=None, message="",
            results=list(state._state["log"]),  # keep whatever finished before the crash
        )
        # A crashed build never offers its zip (zip_path stays None), so its
        # workdir -- with the unfinished archive and any leftovers -- is garbage.
        shutil.rmtree(workdir, ignore_errors=True)
