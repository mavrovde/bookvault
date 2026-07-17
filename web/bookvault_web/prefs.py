"""Server-side shared UI state: which books are selected, and the preferred
ebook/audiobook formats.

These used to live in the browser (localStorage/sessionStorage), which meant
every browser -- and even every tab after a reload -- had its own view. The app
is single-user and its download *progress* is already server-side (see
activity.py), so the selection and format preferences belong there too: with
one source of truth, any browser that opens the app sees the same ticked books,
the same format choices, and the same running download.

Deliberately a flat module-level store, not a class -- exactly one account is
logged in at a time (see session.py/cache.py), so there's nothing to key it by.
Persisted to disk (like the cache) so it also survives a restart; in Docker
that file lives on the /data volume.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Relative to CWD by default; the Docker image pins it onto the /data volume
# (see Dockerfile.web) so it persists across container restarts.
STATE_PATH = Path(os.environ.get("LITRES_STATE_FILE", ".litres_state.json"))

_lock = threading.Lock()
_state: Optional[dict] = None

_DEFAULTS = {"selected": [], "ebook_format": None, "audiobook_format": None}


def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("UI-state file unreadable, starting fresh: %s", exc)
            loaded = {}
    else:
        loaded = {}
    _state = {**_DEFAULTS, **{k: loaded[k] for k in _DEFAULTS if k in loaded}}
    return _state


def _save() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a crash mid-write can't leave a truncated JSON
    # file behind (os.replace is atomic on POSIX and Windows).
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(_state))
    os.replace(tmp, STATE_PATH)


def snapshot() -> dict:
    """A copy of the shared UI state, safe for the caller to embed in a JSON
    response (selected art_ids + the two format preferences)."""
    with _lock:
        state = _load()
        return {
            "selected": list(state["selected"]),
            "ebook_format": state["ebook_format"],
            "audiobook_format": state["audiobook_format"],
        }


def update(
    *,
    selected: Optional[list] = None,
    ebook_format: Optional[str] = None,
    audiobook_format: Optional[str] = None,
) -> dict:
    """Partial update: only the fields passed (non-None) are changed, so a
    caller can push just the selection, or just one format, without clobbering
    the rest. Returns the new snapshot."""
    with _lock:
        state = _load()
        if selected is not None:
            # Normalise to a de-duplicated list of ints, order-stable.
            seen, ids = set(), []
            for x in selected:
                try:
                    i = int(x)
                except (TypeError, ValueError):
                    continue
                if i not in seen:
                    seen.add(i)
                    ids.append(i)
            state["selected"] = ids
        if ebook_format is not None:
            state["ebook_format"] = ebook_format
        if audiobook_format is not None:
            state["audiobook_format"] = audiobook_format
        _save()
        return {
            "selected": list(state["selected"]),
            "ebook_format": state["ebook_format"],
            "audiobook_format": state["audiobook_format"],
        }


def reset() -> None:
    """Drop all shared UI state (used by tests; also safe on logout)."""
    global _state
    with _lock:
        _state = dict(_DEFAULTS)
        STATE_PATH.unlink(missing_ok=True)
