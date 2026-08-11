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
import tempfile
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
    "not_a_folder": "That path is a file, not a folder -- pick a folder instead.",
    "not_writable": "That folder is read-only -- pick one you can write to.",
    "outside_allowed_roots": (
        "Pick a folder in your home directory or on a mounted drive."
    ),
}

_lock = threading.Lock()
_state: dict | None = None

_DEFAULTS = {
    "selected": [],
    "ebook_format": None,
    "audiobook_format": None,
    "download_dir": None,
}


def warn_if_state_is_cwd_relative() -> None:
    """Say so, once at startup, when the shared UI state is being kept
    relative to the working directory (issue #42).

    Launch `bookvault-web` from a different folder and every preference reads
    as unset -- nothing is lost, but the old file simply isn't found, which
    looks exactly like "the setting didn't persist". The packaged desktop app
    and Docker both pin an absolute LITRES_STATE_FILE and never hit this.

    Relocating the default to a per-user data dir is the real fix, but it
    would move existing users' state and needs a migration decision, so this
    only makes the situation visible in the log."""
    if not STATE_PATH.is_absolute():
        logger.warning(
            "Shared UI state is stored at %s, relative to the current working directory "
            "(%s). Launching from elsewhere will read preferences as unset -- set "
            "LITRES_STATE_FILE to an absolute path to pin it.",
            STATE_PATH, Path.cwd(),
        )


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


def allowed_download_roots() -> list[Path]:
    """Directories a save folder may live under.

    Deliberately broad enough for real use -- anywhere in the user's own home,
    a mounted external drive, or the system temp dir -- and no broader. The
    point is not to distrust the user; it's that this setting is reachable by
    an unauthenticated POST from any page they happen to have open (127.0.0.1,
    no CSRF token, by design), so "somewhere a library lives" is a much safer
    ceiling than "anywhere the process can write".

    Module-level so a packaged build or a test can extend it.
    """
    roots = [Path.home(), Path(tempfile.gettempdir())]
    # Where external drives mount, per OS. Harmless when absent.
    roots += [Path("/Volumes"), Path("/media"), Path("/mnt")]
    if DEFAULT_DOWNLOAD_DIR:
        roots.append(Path(DEFAULT_DOWNLOAD_DIR))
    out = []
    for root in roots:
        try:
            out.append(root.resolve())
        except OSError:  # pragma: no cover - unreadable mount point
            continue
    return out


def _confined_to_allowed_roots(path) -> Path | None:
    """The normalized `path` if it sits inside an allowed root, else None.

    The single confinement check, following the scanner's own guidance to the
    letter: **normalize first, then verify the result is within the root.**
    `os.path.realpath` collapses `..` and follows symlinks, so the string we
    check is the exact one the OS would open -- normalizing *after* checking
    would let `<root>/../../etc` slip through a naive prefix test. The
    comparison is `== base` or `startswith(base + os.sep)`, the trailing
    separator stopping `/home/bob-evil` from passing as inside `/home/bob`.

    Returns the realpath'd `Path` (rebuilt from the check, not the caller's raw
    input), so every downstream sink -- a stat, a write -- receives a value
    that has crossed this barrier.

    Applied when a folder is *set* (`_normalise_download_dir`) and every time a
    stored one is *read back* (`_download_dir_warning`, `resolve_download_dir`).
    Re-checking on read is defense in depth: a value valid when saved but now
    outside the roots -- a hand-edited or legacy state file, a changed default
    -- is re-confined rather than trusted."""
    real = os.path.realpath(str(path))
    for root in allowed_download_roots():
        base = os.path.realpath(str(root))
        if real == base or real.startswith(base + os.sep):
            return Path(real)
    return None


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
    # Resolve first, so `..` and symlinks are collapsed and the path checked
    # below is the one the OS will actually open.
    path = Path(text).resolve()
    if not path.is_absolute():  # defensive: resolve() should guarantee this
        raise InvalidDownloadDir("not_absolute")
    # Then constrain it. Normalising alone isn't a defence: it stops traversal
    # tricks but not "write my archive into ~/Library/LaunchAgents". This value
    # arrives over HTTP, and the app binds 127.0.0.1 with no auth and no CSRF
    # token by design -- so any page the user happens to be visiting can POST
    # here. Confining writes to the places a library plausibly lives turns that
    # from "choose any directory on the machine" into "choose a folder".
    # Confine before any filesystem access. `_confined_to_allowed_roots`
    # normalizes then checks containment (the scanner's recommended order) and
    # returns a path rebuilt from that check, so `safe_path` -- not the raw
    # user string -- is what every stat below touches.
    safe_path = _confined_to_allowed_roots(path)
    if safe_path is None:
        # The code, not the path: this is the one validation branch a hostile
        # page can drive, and the log shouldn't become a place to read back
        # what it probed for. The other rejections log the same way below.
        logger.info("Rejected a save folder: outside the allowed roots")
        raise InvalidDownloadDir("outside_allowed_roots")

    # From here `safe_path` is known to sit under one of those roots.
    # Folders only. `resolve()` above already followed any symlink, so this
    # also catches a link pointing at a file. A path that doesn't exist yet is
    # fine and stays fine -- _save_archive mkdirs it when a build finishes --
    # but it gets a warning (see _download_dir_warning) so a typo doesn't
    # surface for the first time at the end of a multi-gigabyte build.
    if safe_path.exists():
        if not safe_path.is_dir():
            logger.info("Rejected a save folder: it is a file, not a folder")
            raise InvalidDownloadDir("not_a_folder")
        # An existing but read-only folder fails at the very last step of a
        # build, after everything is downloaded. Refuse it now instead.
        if not os.access(safe_path, os.W_OK):
            logger.info("Rejected a save folder: not writable")
            raise InvalidDownloadDir("not_writable")
    else:
        logger.info("Accepted a save folder that does not exist yet -- it will be created on save")
    return str(safe_path)


def validate_download_dir(value: str) -> str | None:
    """Check a folder the same way a saved one is checked, without storing it.

    For one-off destinations -- "save a copy of this zip over there" -- which
    must clear exactly the same bar as the configured save folder (absolute,
    a real folder, writable, inside the allowed roots) but must NOT become the
    configured folder as a side effect. Raises InvalidDownloadDir; returns None
    only for an empty value."""
    return _normalise_download_dir(value)


def _download_dir_warning(state: dict) -> str | None:
    """A non-blocking caveat about the destination in force.

    Distinct from InvalidDownloadDir: that rejects a value outright, this
    accepts it and says what to expect. It's derived on read (like
    `download_dir_effective`) rather than stored, so it also covers a
    destination that came from LITRES_DOWNLOAD_DIR and one that was fine when
    it was set but has since been deleted or unmounted."""
    effective = _effective_download_dir(state)
    if not effective:
        return None
    # Re-confine before stating it: a stored value can outlive the guard that
    # accepted it (a hand-edited state file, a default that has since moved),
    # and this runs on every /activity poll, so it must never stat a path that
    # is no longer inside the allowed roots.
    path = _confined_to_allowed_roots(effective)
    if path is None:
        return "This folder is no longer an allowed location -- pick another."
    try:
        if not path.exists():
            return "This folder doesn't exist yet -- it will be created when a build finishes."
        if not path.is_dir():
            return "This is a file, not a folder -- the archive can't be saved here."
        if not os.access(path, os.W_OK):
            return "This folder is read-only -- the archive can't be saved here."
    except OSError:  # pragma: no cover -- e.g. an unreachable network mount
        return "This folder can't be reached right now."
    return None


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
    if not effective:
        return None
    # The archive is written here, so this is the highest-value sink of all --
    # re-confine even though the value was checked when stored, in case the
    # stored state was tampered with or predates the guard. A value that no
    # longer sits inside the allowed roots falls back to keeping the archive in
    # its temp workdir (None) rather than writing somewhere unexpected.
    confined = _confined_to_allowed_roots(effective)
    if confined is None:
        logger.warning("Stored save folder is outside the allowed roots; keeping the archive in temp")
    return confined


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
            "download_dir_warning": _download_dir_warning(state),
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
            "download_dir_warning": _download_dir_warning(state),
        }


def reset() -> None:
    """Drop all shared UI state (used by tests; also safe on logout)."""
    global _state
    with _lock:
        _state = dict(_DEFAULTS)
        STATE_PATH.unlink(missing_ok=True)
