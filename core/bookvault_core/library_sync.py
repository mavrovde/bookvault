"""Orchestrate syncing a Litres library onto an ABS-compatible disk tree.

All LitresClient I/O is expected to run on session.py's Playwright worker
thread (callers submit this module's functions via session.run/submit).
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from bookvault_core import cache
from bookvault_core.client import DownloadCancelled, LitresClient
from bookvault_core.library_fs import (
    abs_metadata,
    book_dir_for_art,
    install_book,
    is_up_to_date,
    pick_last_release,
)

logger = logging.getLogger(__name__)

ProgressCb = Callable[[str, int, int], None] | None


def _is_audio(art_or_meta: dict) -> bool:
    if "is_audio" in art_or_meta and art_or_meta["is_audio"] is not None:
        return bool(art_or_meta["is_audio"])
    return art_or_meta.get("art_type") == 1


def _normalize_art(art: dict) -> dict:
    """Library list raw art or already-normalized dict → normalized shape."""
    if "persons" in art or "art_type" in art or "language_code" in art:
        return LitresClient.normalize_library_item(art)
    if isinstance(art.get("authors"), list):
        return art
    authors_raw = art.get("authors") or ""
    if isinstance(authors_raw, str):
        authors = [a.strip() for a in authors_raw.split(",") if a.strip()] or ["Unknown"]
    else:
        authors = list(authors_raw) or ["Unknown"]
    return {
        "id": art.get("id"),
        "title": art.get("title") or str(art.get("id") or ""),
        "subtitle": art.get("subtitle") or "",
        "authors": authors,
        "narrators": list(art.get("narrators") or []),
        "series": art.get("series"),
        "series_list": art.get("series_list") or [],
        "is_audio": bool(art.get("is_audio")),
        "art_type": 1 if art.get("is_audio") else art.get("art_type"),
        "language_code": art.get("language_code"),
        "last_released_at": art.get("last_released_at"),
        "last_updated_at": art.get("last_updated_at"),
        "purchased_at": art.get("purchased_at"),
        "is_adult_content": bool(art.get("is_adult_content")),
    }


def _enrich_details(client: LitresClient, art_id, should_cancel=None) -> dict | None:
    get_art = getattr(client, "get_art", None)
    if get_art is None:
        return None
    try:
        raw = get_art(art_id, should_cancel=should_cancel)
        return LitresClient.normalize_art_details(raw, files=None)
    except Exception as exc:  # noqa: BLE001 -- detail metadata is a bonus; the listing row is enough to install
        logger.info("Detail fetch failed for art %s (continuing with list metadata): %s", art_id, exc)
        return None


def sync_one(
    client: LitresClient,
    library_root: Path,
    art: dict,
    *,
    preferred_ext: str | None = None,
    preferred_file_type: str | None = None,
    should_cancel=None,
    workdir: Path | None = None,
) -> dict:
    """Download/install a single art. Returns a log row dict."""
    meta = _normalize_art(art)
    art_id = meta.get("id")
    title = meta.get("title") or str(art_id)
    last = pick_last_release(meta)
    abs_meta = abs_metadata(meta)
    dest_dir = book_dir_for_art(library_root, abs_meta)

    if should_cancel is not None and should_cancel():
        return {"title": title, "status": "cancelled", "id": art_id}

    if last and is_up_to_date(dest_dir, last):
        return {
            "title": title,
            "status": "skipped",
            "reason": "up_to_date",
            "id": art_id,
            "path": str(dest_dir),
        }

    details = _enrich_details(client, art_id, should_cancel=should_cancel)
    if details:
        abs_meta = abs_metadata(meta, details)
        last = abs_meta.get("last_release") or last
        dest_dir = book_dir_for_art(library_root, abs_meta)
        if last and is_up_to_date(dest_dir, last):
            return {
                "title": title,
                "status": "skipped",
                "reason": "up_to_date",
                "id": art_id,
                "path": str(dest_dir),
            }

    if should_cancel is not None and should_cancel():
        return {"title": title, "status": "cancelled", "id": art_id}

    try:
        files = cache.get_files(art_id)
        if files is None:
            files = client.get_files(art_id, should_cancel=should_cancel)
            cache.set_files(art_id, files)
        best = client.pick_best_file(files, preferred_ext, preferred_file_type)
        if best is None:
            return {
                "title": title,
                "status": "skipped",
                "reason": "No downloadable file for this title on litres.ru (rights-limited or preview-only).",
                "id": art_id,
            }

        ext = client.file_extension(best)
        own_workdir = workdir is None
        wd = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="bookvault-sync-"))
        wd.mkdir(parents=True, exist_ok=True)
        tmp = wd / f"{art_id}.{ext}"
        try:
            client.download_file(
                art_id,
                best["id"],
                tmp.name,
                tmp,
                should_cancel=should_cancel,
            )
            if should_cancel is not None and should_cancel():
                tmp.unlink(missing_ok=True)
                return {"title": title, "status": "cancelled", "id": art_id}

            path = install_book(
                library_root,
                abs_meta,
                tmp,
                is_audio=_is_audio(meta),
            )
        finally:
            if own_workdir:
                shutil.rmtree(wd, ignore_errors=True)

        return {
            "title": title,
            "status": "done",
            "id": art_id,
            "path": str(path),
            "ext": ext,
        }
    except DownloadCancelled:
        return {"title": title, "status": "cancelled", "id": art_id}
    except Exception as exc:  # noqa: BLE001 -- one title failing must not sink a whole-library sync
        name = type(exc).__name__
        if "Cancel" in name:
            return {"title": title, "status": "cancelled", "id": art_id}
        logger.warning("Sync failed for %r (art %s): %s", title, art_id, exc)
        return {
            "title": title,
            "status": "error",
            "id": art_id,
            "error": str(exc)[:300],
        }


def sync_library(
    client: LitresClient,
    library_root: Path,
    *,
    audio_only: bool = True,
    preferred_ext: str | None = None,
    preferred_file_type: str | None = None,
    should_cancel=None,
    on_progress: ProgressCb = None,
    art_ids: set | None = None,
) -> dict:
    """Walk the purchased library and install missing/outdated titles.

    Returns ``{done, skipped, failed, cancelled, total, log, library_root}``.
    """
    library_root = Path(library_root)
    library_root.mkdir(parents=True, exist_ok=True)

    # Default page size: `limit` is the per-page parameter sent to litres.ru,
    # not a cap on results -- iter_library paginates until the library is
    # exhausted either way. Asking for a huge page just makes an odd request.
    arts = list(client.iter_library())
    if art_ids is not None:
        arts = [a for a in arts if a.get("id") in art_ids]

    selected = []
    for art in arts:
        meta = _normalize_art(art)
        if audio_only and not _is_audio(meta):
            continue
        selected.append(art)

    total = len(selected)
    log: list[dict] = []
    done = skipped = failed = cancelled = 0

    for idx, art in enumerate(selected):
        if should_cancel is not None and should_cancel():
            cancelled += 1
            break
        title = art.get("title") or str(art.get("id"))
        if on_progress is not None:
            on_progress(str(title), idx, total)

        row = sync_one(
            client,
            library_root,
            art,
            preferred_ext=preferred_ext,
            preferred_file_type=preferred_file_type,
            should_cancel=should_cancel,
        )
        log.append(row)
        status = row.get("status")
        if status == "done":
            done += 1
        elif status == "skipped":
            skipped += 1
        elif status == "cancelled":
            cancelled += 1
            break
        else:
            failed += 1

    if on_progress is not None:
        on_progress("", total, total)

    return {
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "cancelled": cancelled,
        "total": total,
        "log": log,
        "library_root": str(library_root),
    }
