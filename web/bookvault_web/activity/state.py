"""The activity state machine itself: the states, the shared mutable state,
and the transitions between them.

Everything the app can be *doing* passes through here. The domain modules
(`library`, `mirror`, `archive`, `abs_sync`) own the work; this module owns
the answer to "may it start, and what is running now".

The single dedicated Playwright worker thread (see `core/session.py`) means
at most one activity can run at a time. `_begin()` makes that explicit rather
than implicit -- it is the only way into a non-IDLE state, and it refuses if
one is already in flight.

    IDLE -> REFRESHING  -> (CHECKING) -> IDLE     (reload the library list)
    IDLE -> CHECKING                  -> IDLE     (paced per-book size sweep)
    IDLE -> PREPARING                 -> IDLE     (build the download zip)
    IDLE -> DOWNLOADING               -> IDLE     (loose-file mirror of the selection)
    IDLE -> SYNCING                   -> IDLE     (on-disk ABS library sync)
    CHECKING/PREPARING/DOWNLOADING/SYNCING -> STOPPING -> IDLE (cancel)

**Adding a long-running state means adding it to `cancel()`**, or Stop is
silently a no-op for it -- which has shipped as a bug once already.

Cancellation is cooperative: loops check `_cancel_event` between books, and
`client.download_file` polls it between streamed chunks, so a Stop interrupts
even a mid-transfer audiobook within a fraction of a second.
"""
from __future__ import annotations

import logging
import threading

from bookvault_core.client import LitresBlocked

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

# Activities that write their own per-book results, and therefore supersede the
# previous run's the moment they start. REFRESHING and CHECKING are absent on
# purpose: they are read-only, and the size-check that fires on every page load
# must never wipe the results a finished build left behind.
PRODUCES_RESULTS = (PREPARING, DOWNLOADING, SYNCING)

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
    "log": [],              # per-book results of the RUNNING archive.prepare: {"title","status",...}
    "results": [],          # durable copy of the last finished archive.prepare's log, so the
                            # results view (and its failed/skipped filter) survives the
                            # size-check that fires on the next page load. Only a NEW
                            # prepare replaces it -- _begin deliberately leaves it alone.
    "error": None,          # raw-ish error line for the UI when result == "error"
    "sizes": {},            # {art_id: size_mb|None} resolved during a sweep, for the UI to paint rows
    "zip_path": None,       # path to the built zip, for the /download/file route
    "saved_path": None,     # where the archive was auto-saved (the configured download
                            # folder), or None when it stayed in its temp workdir. Durable
                            # alongside zip_path so the "Saved to ..." line survives a reload.
    "workdir": None,        # temp dir of the last build, kept ONLY so the next archive.prepare() can
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
        if state in PRODUCES_RESULTS:
            _state["results"] = []
        if state == PREPARING:
            # The archive itself is being replaced, and the previous one may
            # have lived in a temp workdir that prepare() is about to delete --
            # so its link would dangle. (A files download leaves an earlier
            # zip alone: it is a different artefact, still on disk, and still
            # the user's.)
            _state["zip_path"] = None
            _state["saved_path"] = None
    # Note: `zip_path`, `saved_path`, `results` and `sizes` are intentionally
    # NOT reset for a *read-only* activity, so a finished build's download
    # link, "Saved to ..." line and results view survive the size-check that
    # fires on the next page load.
    #
    # A run that produces its own results is the opposite case: the moment it
    # starts, the previous run's per-book list describes work that has been
    # superseded, and leaving it on screen reads as though it belongs to the
    # run now in progress. `prepare()` cleared it itself; `download_files()`
    # and `start_sync()` did not, so starting either left the last build's
    # results sitting under a fresh progress bar. Deciding it here means the
    # next activity added cannot forget to.
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
        return "Session changed while this was running (e.g. a login/logout) -- library.refresh the page and retry."
    if "socket hang up" in lower or "econnreset" in lower:
        return "Connection to litres.ru was interrupted -- wait a bit, then retry."
    return f"Download failed: {text[:150]}"
