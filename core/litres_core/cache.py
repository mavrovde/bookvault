"""Disk-backed cache for read-mostly litres.ru API responses: the library
listing and each book's file listing (used for both size display and
downloads).

This isn't just a speed optimization -- litres.ru's DDoS-Guard anti-bot
check reacts to bulk/repeated request patterns, and this app naturally
produces exactly that shape of traffic: every page load re-fetches the
whole library, and background size checks hit every book's file listing.
A purchased book's available file formats effectively never change, and
the library itself only changes when the user buys something new, so both
are safe to cache aggressively -- this means a page reload, an app
restart, or even starting a download right after browsing the library
doesn't need to touch litres.ru at all when the cache is warm.

Deliberately a flat module-level cache, not a class -- there's exactly one
account logged in at a time (see session.py), so there's nothing to key
the cache by beyond the data's own ids. `clear()` is called on
login/logout so a different account's data never bleeds into the next.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Relative to the current working directory by default (repo root when
# launched from there); set LITRES_CACHE_FILE to an absolute path to pin it.
CACHE_PATH = Path(os.environ.get("LITRES_CACHE_FILE", ".litres_cache.json"))

# The library listing changes whenever the user buys/removes something, so
# it's refreshed fairly eagerly (and can always be forced via the UI's
# Refresh button). A book's file listing/formats essentially never change
# once purchased, so that can be cached much longer.
LIBRARY_TTL = int(os.environ.get("LITRES_LIBRARY_CACHE_TTL", str(15 * 60)))
FILES_TTL = int(os.environ.get("LITRES_FILES_CACHE_TTL", str(7 * 24 * 60 * 60)))

_lock = threading.Lock()
_state: Optional[dict] = None


def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    if CACHE_PATH.exists():
        try:
            _state = json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Cache file unreadable, starting fresh: %s", exc)
            _state = {}
    else:
        _state = {}
    _state.setdefault("library", None)
    _state.setdefault("files", {})
    return _state


def _save() -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(_state))


def get_library() -> Optional[list]:
    """Return the cached library listing (the web UI's book-list shape) if
    still fresh, else None."""
    with _lock:
        entry = _load().get("library")
        if entry and time.time() - entry["fetched_at"] < LIBRARY_TTL:
            return entry["books"]
        return None


def get_library_stale() -> Optional[list]:
    """Return the cached library listing regardless of TTL freshness, or None
    if nothing was ever cached. A live re-fetch runs on the single Playwright
    worker thread (see session.py); when that thread is busy with a long
    activity (e.g. a large download), serving this slightly-stale list is far
    better than blocking the request until the activity finishes."""
    with _lock:
        entry = _load().get("library")
        return entry["books"] if entry else None


def set_library(books: list) -> None:
    with _lock:
        state = _load()
        state["library"] = {"fetched_at": time.time(), "books": books}
        _save()
    logger.info("Cached library listing: %d book(s)", len(books))


def get_files(art_id) -> Optional[list]:
    """Return the cached file listing for one book if still fresh, else
    None. Keyed by string since JSON object keys are always strings."""
    with _lock:
        entry = _load()["files"].get(str(art_id))
        if entry and time.time() - entry["fetched_at"] < FILES_TTL:
            return entry["files"]
        return None


def set_files(art_id, files: list) -> None:
    with _lock:
        state = _load()
        state["files"][str(art_id)] = {"fetched_at": time.time(), "files": files}
        _save()


def clear() -> None:
    """Drop all cached data -- the cache holds one account's data at a
    time, so this runs on login/logout to avoid leaking a previous
    account's library/files into a new session."""
    global _state
    with _lock:
        _state = {"library": None, "files": {}}
        CACHE_PATH.unlink(missing_ok=True)
    logger.info("Cache cleared")
