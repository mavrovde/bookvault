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

logger = logging.getLogger(__name__)

# Relative to CWD by default; the Docker image pins it onto the /data volume
# (see Dockerfile.web) so it persists across container restarts.
STATE_PATH = Path(os.environ.get("LITRES_STATE_FILE", ".litres_state.json"))

def _system_download_dir() -> str:
    """The OS's own Downloads folder -- the destination when the user hasn't
    chosen one and no LITRES_DOWNLOAD_DIR is set. Linux desktops let the user
    rename/move it (XDG user dirs), so honour that before assuming the
    English default; macOS and Windows both use ~/Downloads."""
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return os.path.expandvars(os.path.expanduser(xdg))
    user_dirs = Path.home() / ".config" / "user-dirs.dirs"
    try:
        for line in user_dirs.read_text().splitlines():
            if line.startswith("XDG_DOWNLOAD_DIR="):
                raw = line.split("=", 1)[1].strip().strip('"')
                # Entries look like "$HOME/Downloads"
                return os.path.expandvars(os.path.expanduser(raw))
    except OSError:
        pass  # no XDG config (macOS/Windows, or a bare Linux) -- use the default
    return str(Path.home() / "Downloads")


# Where a finished archive is saved when the user hasn't picked a folder.
# Read at import (like STATE_PATH) so tests and the frozen app can override the
# attribute. LITRES_DOWNLOAD_DIR is already set by packaging/entry.py for the
# desktop app and honoured by the MCP server; failing that, the archive lands
# in the system Downloads folder rather than a temp directory nobody can find.
DEFAULT_DOWNLOAD_DIR = os.environ.get("LITRES_DOWNLOAD_DIR") or _system_download_dir()


class InvalidDownloadDir(ValueError):
    """The save folder the user typed can't be used.

    Carries a `code`, not a message: the route answers with the fixed string
    DOWNLOAD_DIR_ERRORS maps that code to, so nothing derived from the
    exception (which can carry filesystem detail) is ever echoed back over
    HTTP. Subclasses ValueError so callers can still catch it broadly."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# The only strings the /prefs route will send back for a rejected folder.
DOWNLOAD_DIR_ERRORS = {
    "not_absolute": "Please give a full folder path (starting with / or ~).",
    "not_a_folder": "That path is a file, not a folder.",
}

_lock = threading.Lock()
_state: dict | None = None

_DEFAULTS = {
    "selected": [],
    "ebook_format": None,
    "audiobook_format": None,
    "download_dir": None,
}


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


def _normalise_download_dir(value: str) -> str | None:
    """Turn what the user typed into a storable absolute path, or None when
    they cleared the field. Raises ValueError on anything unusable so the
    route can answer 400 instead of silently saving a path that will fail at
    the end of a multi-gigabyte build."""
    text = os.path.expanduser(str(value).strip())
    if not text:
        return None  # cleared -- fall back to the system/env default
    # A NUL truncates the path at the C level, so what gets validated here and
    # what the OS eventually opens can differ. Refuse outright.
    if "\x00" in text:
        raise InvalidDownloadDir("not_absolute")
    if not os.path.isabs(text):
        raise InvalidDownloadDir("not_absolute")
    # Resolve before storing: this value arrives over HTTP and is later used to
    # build (and write) a multi-gigabyte archive. The app is 127.0.0.1-only and
    # has no auth or CSRF token, so a page the user happens to be visiting can
    # POST here -- normalising `..` and symlinks means the stored path is the
    # one that was actually checked, not a traversal that resolves elsewhere.
    path = Path(text).resolve()
    if not path.is_absolute():  # defensive: resolve() should guarantee this
        raise InvalidDownloadDir("not_absolute")
    if path.exists() and not path.is_dir():
        raise InvalidDownloadDir("not_a_folder")
    return str(path)


def _effective_download_dir(state: dict) -> str | None:
    """The destination actually in force: the folder the user chose, else
    LITRES_DOWNLOAD_DIR, else the system Downloads folder."""
    return state["download_dir"] or DEFAULT_DOWNLOAD_DIR


def resolve_download_dir() -> Path | None:
    """Where a finished archive should be saved. The one place the
    pref/env/system fallback is decided. None only if the default itself has
    been cleared (tests do this to keep the archive in its temp workdir)."""
    with _lock:
        effective = _effective_download_dir(_load())
    return Path(effective) if effective else None


def snapshot() -> dict:
    """A copy of the shared UI state, safe for the caller to embed in a JSON
    response (selected art_ids, the two format preferences, and the archive
    destination). `download_dir_effective` is derived, not stored -- it lets
    the UI show the env-provided default without reimplementing the fallback."""
    with _lock:
        state = _load()
        return {
            "selected": list(state["selected"]),
            "ebook_format": state["ebook_format"],
            "audiobook_format": state["audiobook_format"],
            "download_dir": state["download_dir"],
            "download_dir_effective": _effective_download_dir(state),
        }


def update(
    *,
    selected: list | None = None,
    ebook_format: str | None = None,
    audiobook_format: str | None = None,
    download_dir: str | None = None,
) -> dict:
    """Partial update: only the fields passed (non-None) are changed, so a
    caller can push just the selection, or just one format, without clobbering
    the rest. Returns the new snapshot.

    `download_dir=""` is the exception that proves the rule: None already means
    "leave alone", so the empty string is how a caller clears the pref back to
    the LITRES_DOWNLOAD_DIR default. Raises ValueError on an unusable path."""
    # Validate before taking the lock: a rejected path must not leave the
    # other fields of a multi-field update half-applied in memory.
    new_dir = _normalise_download_dir(download_dir) if download_dir is not None else None
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
        if download_dir is not None:
            state["download_dir"] = new_dir
        _save()
        return {
            "selected": list(state["selected"]),
            "ebook_format": state["ebook_format"],
            "audiobook_format": state["audiobook_format"],
            "download_dir": state["download_dir"],
            "download_dir_effective": _effective_download_dir(state),
        }


def reset() -> None:
    """Drop all shared UI state (used by tests; also safe on logout)."""
    global _state
    with _lock:
        _state = dict(_DEFAULTS)
        STATE_PATH.unlink(missing_ok=True)
