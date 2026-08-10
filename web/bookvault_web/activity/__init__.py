"""The single backend state machine for everything the app can be *doing*.

Historically this logic lived in the browser: app.js owned an `activity` enum,
a `stopRequested` flag, and the entire paced size-check loop, while the backend
only tracked the download job's own status. That split meant the rules for
"what can run at once / which button is live / when to pace a request" were
spread across the frontend and had to be re-derived there. They belong here.

**Layout.** This was one 1300-line module; it is now one module per domain,
because the domains are what change independently:

    state.py     the machine: states, shared mutable state, _begin/cancel
    library.py   REFRESHING + CHECKING -- the listing and size sweeps
    mirror.py    DOWNLOADING -- the loose-file mirror, and "do I have this?"
    archive.py   PREPARING -- the zip build and the finished archive
    abs_sync.py  SYNCING -- the Audiobookshelf-shaped on-disk library

This module is the façade: `app.py` and `autosync.py` import `activity` and
use the names below, so callers are unaffected by where the code lives.

**Testing note, and it is not optional.** Cross-module references go through
the *module object* (`library._iter_books(...)`, `state._state`), never
`from .library import _iter_books`. A `from` import binds the function at
import time, so `monkeypatch.setattr` on the owning module would not be seen
by the caller -- and the test would pass while injecting nothing, which is
strictly worse than failing. For the same reason, patch the module that
*owns* a name (`activity.library.PACE_SECONDS`), not this façade: rebinding it
here changes only this module's copy.
"""
from __future__ import annotations

from . import abs_sync, archive, library, mirror, state
from .abs_sync import start_sync
from .archive import copy_archive_to, prepare
from .library import build_books, check_sizes, fetch_size, refresh, size_of_files
from .mirror import books_on_disk, download_files, forget_books_on_disk
from .state import (
    CHECKING,
    DOWNLOADING,
    IDLE,
    PREPARING,
    REFRESHING,
    STOPPING,
    SYNCING,
    cancel,
    forget_sizes,
    snapshot,
)

# The states, the machine (snapshot/cancel/forget_sizes), the library helpers,
# the four activities -- plus the domain modules themselves, exported so tests
# can patch the module that *owns* a name rather than this façade's copy.
__all__ = [
    "CHECKING",
    "DOWNLOADING",
    "IDLE",
    "PREPARING",
    "REFRESHING",
    "STOPPING",
    "SYNCING",
    "abs_sync",
    "archive",
    "books_on_disk",
    "build_books",
    "cancel",
    "check_sizes",
    "copy_archive_to",
    "download_files",
    "fetch_size",
    "forget_books_on_disk",
    "forget_sizes",
    "library",
    "mirror",
    "prepare",
    "refresh",
    "size_of_files",
    "snapshot",
    "start_sync",
    "state",
]
