"""Minimal local, single-user web app: log in once, click a button, get your
whole litres.ru library as a zip.

Intentionally bound to 127.0.0.1 only (see run.py) -- this is a personal
tool for the account owner, not a multi-user service.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from urllib.parse import urlparse

import anyio
from bookvault_core import cache, session
from bookvault_core.client import (
    AUDIOBOOK_FILE_TYPES,
    EBOOK_EXTENSIONS,
    LitresAuthError,
    LitresBrowserUnavailable,
)
from bookvault_core.library_fs import library_root_from_env
from fastapi import FastAPI, Form
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import activity, autosync, folder_dialog, prefs

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Make sure app-level logging is configured wherever this module is
    imported from.

    `run.main()` calls basicConfig, but uvicorn's `--reload` (the default for a
    local run) serves from a **subprocess** that imports
    `bookvault_web.app:app` directly and never executes `main()`. The root
    logger there stays unconfigured, so Python's handler-of-last-resort prints
    WARNING and above unformatted and silently drops every INFO line -- which
    is how the folder picker, session restore and download progress all went
    missing from the log in exactly the mode developers actually run.

    No-op when handlers already exist, so it never fights `run.main()`, the
    Docker entrypoint, or a test's caplog."""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.environ.get("LITRES_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


_configure_logging()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sync Playwright refuses to run inside an asyncio loop, and FastAPI's
    # lifespan runs directly on the event loop thread -- push it to a
    # worker thread, same as Starlette does for sync route handlers.
    #
    # allow_env_login=False: the web app never auto-logs-in from .env
    # credentials -- it restores a saved session (or re-logs-in from the OS
    # keychain), and otherwise shows its login form. LITRES_LOGIN/PASSWORD
    # in .env are for the headless MCP server only (see session.py).
    prefs.warn_if_state_is_cwd_relative()
    await anyio.to_thread.run_sync(partial(session.restore_session, allow_env_login=False))
    autosync.start_background_scheduler(session.current_client, prefs.snapshot)
    try:
        yield
    finally:
        autosync.stop_background_scheduler()
        await anyio.to_thread.run_sync(session.shutdown)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# Verbs that change something. GETs stay open: they're what a bookmark, the
# desktop window, and the live smoke tests all use, and none of them mutate.
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def block_cross_origin_writes(request: Request, call_next):
    """Refuse state-changing requests that a *foreign page* made (issue #41).

    The app binds 127.0.0.1 with no auth and no CSRF token -- deliberate for a
    single-user local tool, but it means any website the user happens to have
    open can POST here: start a multi-gigabyte build, fire a library-wide sweep
    that gets the account anti-bot flagged, change the save folder, log them
    out. Nothing escalates privileges, but none of it should be a page's to
    trigger.

    Checked in two steps, most reliable first:

    1. `Sec-Fetch-Site` -- set by the browser itself and unforgeable by page
       JS. `same-origin` is our own UI; `none` is a typed URL or a bookmark.
       Anything else (`cross-site`, `same-site`) is another page and is out.
    2. `Origin` -- the fallback for engines that don't send Sec-Fetch-Site
       (older WebKitGTK, which the Linux desktop build runs on). A browser
       always sends Origin on a cross-origin POST, so a mismatch is decisive.

    Neither header present means the caller isn't a browser at all -- curl, the
    live smoke tests, a local script. Those are allowed through: the threat
    model here is a web page the user visited, not code already running as the
    user, which could talk to the app anyway.
    """
    if request.method in _STATE_CHANGING:
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if site is not None and site not in ("same-origin", "none"):
            logger.warning("Refused a %s %s from a %s page", request.method, request.url.path, site)
            return JSONResponse(
                {"ok": False, "error": "Cross-origin requests are not allowed."}, status_code=403
            )
        if site is None and origin is not None and urlparse(origin).netloc != request.headers.get("host"):
            logger.warning("Refused a %s %s from a foreign origin", request.method, request.url.path)
            return JSONResponse(
                {"ok": False, "error": "Cross-origin requests are not allowed."}, status_code=403
            )
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    saved = prefs.snapshot()
    library_root = library_root_from_env()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "logged_in": session.current_client() is not None,
            "login": session.current_login(),
            "error": None,
            "ebook_formats": EBOOK_EXTENSIONS,
            "audiobook_formats": AUDIOBOOK_FILE_TYPES,
            # Server-side prefs, so the initial HTML already shows the saved
            # choices (no flash before app.js hydrates the rest).
            "ebook_format": saved["ebook_format"],
            "audiobook_format": saved["audiobook_format"],
            "download_dir": saved["download_dir"],
            "download_dir_effective": saved["download_dir_effective"],
            "library_sync_enabled": library_root is not None,
            "library_dir": str(library_root) if library_root else None,
            # Hides the Browse button where no dialog can be drawn (Docker,
            # plain SSH) -- the text field stays as the way in there.
            "folder_picker": folder_dialog.is_available(),
            "asset_v": asset_version(),
        },
    )


_STATIC_DIR = Path(__file__).parent / "static"


def asset_version() -> str:
    """Cache-busting token for the stylesheet and script.

    They're served with an ETag but no Cache-Control, so browsers fall back to
    *heuristic* caching and may reuse them without revalidating. After an
    upgrade that leaves a tab running the previous JS against the new HTML --
    which doesn't look like a caching problem, it looks like a broken control:
    anything the old script doesn't know about keeps whatever state the markup
    gave it, while its neighbours update. A newly added button sitting disabled
    next to a working one is the classic symptom.

    Derived from the newest mtime under static/, so it changes exactly when the
    assets do -- during development as well as across releases, which a version
    number wouldn't. Two stats per page load, and page loads are rare."""
    try:
        newest = max(p.stat().st_mtime for p in _STATIC_DIR.rglob("*") if p.is_file())
    except (OSError, ValueError):  # pragma: no cover - no static dir
        return "0"
    return str(int(newest))


def _login_page(request: Request, error: str, *, status_code: int):
    """Re-render the logged-out page with an error banner.

    The template's logged-out branch only reads `logged_in`/`login`/`error`,
    so the format/prefs context the logged-in branch needs is deliberately
    omitted here (undefined is falsy in Jinja)."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"logged_in": False, "login": None, "error": error, "asset_v": asset_version()},
        status_code=status_code,
    )


@app.post("/login")
def do_login(request: Request, login: str = Form(...), password: str = Form(...)):
    try:
        session.login(login, password)
    except LitresBrowserUnavailable as exc:
        # A local setup problem (Chromium not installed), not a bad password
        # -- so it gets its own banner with the fix, and a 503 rather than a
        # 401. Checked before LitresAuthError: it's a subclass of it.
        logger.warning("Login could not start a browser: %s", exc)
        return _login_page(request, str(exc), status_code=503)
    except LitresAuthError as exc:
        logger.warning("Login attempt failed for %s: %s", login, exc)
        return _login_page(request, str(exc), status_code=401)
    except Exception:
        # Anything else at all: the user gets a readable banner on the login
        # page instead of Starlette's "Internal Server Error" plain-text page,
        # which tells them nothing and loses the form. The traceback goes to
        # the log (exc_info), never into the response -- same contract as
        # /library's 503 above.
        logger.exception("Unexpected error during login for %s", login)
        return _login_page(
            request,
            "Something went wrong while signing in. The details are in the app's "
            "log; wait a moment and try again.",
            status_code=503,
        )
    logger.info("Login attempt succeeded for %s", login)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def do_logout():
    logger.info("Logout requested for %s", session.current_login())
    session.logout()
    # Sizes outlive an activity now, so they have to be dropped explicitly
    # here -- session.logout() clears the on-disk cache they came from, and
    # this clears the in-memory copy that would otherwise carry over.
    activity.forget_sizes()
    return RedirectResponse("/", status_code=303)


@app.get("/library")
def get_library(refresh: bool = False):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    if not refresh:
        cached = cache.get_library()
        if cached is not None:
            return {"ok": True, "books": cached}
        # Fresh cache expired. A live re-fetch runs on the single Playwright
        # worker thread (session.py); if that thread is mid-activity (e.g. a
        # large download), the fetch would block for the whole activity and
        # the library would appear to vanish on any page load/reload. Serve
        # the slightly-stale list instead -- it's still the user's library,
        # just possibly missing a brand-new purchase until the activity ends
        # and a refresh runs.
        if activity.snapshot()["state"] != activity.IDLE:
            stale = cache.get_library_stale()
            if stale is not None:
                return {"ok": True, "books": stale}
    try:
        books = session.run(activity.build_books, client)
    except Exception as exc:  # noqa: BLE001 -- any backend failure must surface as a clean 503, never a raw traceback
        # A transient network blip, an anti-bot block, or a session that
        # was replaced (logout/re-login) mid-request should surface as a
        # clean error the frontend can retry -- not a raw 500 with a
        # traceback (the client object itself may be a stale, already-
        # closed one at this point; see session.py's docstring).
        logger.warning("Library fetch failed: %s", exc)
        return JSONResponse({"ok": False, "error": "Could not load your library -- try again in a moment."}, status_code=503)
    cache.set_library(books)
    return {"ok": True, "books": books}


@app.get("/library/{art_id}/size")
def get_book_size(art_id: int):
    # A single book's size, on demand. Deliberately not part of /library:
    # fetching every book's file size upfront would mean one extra API call
    # per book (this backend has a single dedicated worker thread -- see
    # session.py -- so that's fully sequential). The bulk equivalent is the
    # CHECKING activity (bookvault_web/activity.py), which sweeps sizes in the
    # background; this route stays for one-off/programmatic lookups.
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    cached_files = cache.get_files(art_id)
    if cached_files is not None:
        # No need to even touch the dedicated Playwright thread for a cache
        # hit -- this can run entirely on the request's own async handler,
        # so it stays instant even while that thread is busy downloading.
        return {"ok": True, "size_mb": activity.size_of_files(cached_files), "cached": True}
    try:
        size_mb, files = session.run(activity.fetch_size, client, art_id)
    except Exception as exc:  # noqa: BLE001 -- a failed size fetch just leaves that book's size unknown
        # Best-effort -- a failed size fetch just leaves that book's size
        # unknown; a clean error here is enough, no need to retry serverside.
        logger.info("Size fetch failed for art %s: %s", art_id, exc)
        return JSONResponse({"ok": False, "error": "Could not fetch size"}, status_code=503)
    cache.set_files(art_id, files)
    return {"ok": True, "size_mb": size_mb, "cached": False}


# --------------------------------------------------------------------------
# Activity: the single backend state machine (see bookvault_web/activity.py). The UI
# starts an activity via one of the POST routes below and then polls
# GET /activity to render whatever state it reports -- it owns no
# activity/progress logic of its own.
# --------------------------------------------------------------------------


class PrepareRequest(BaseModel):
    art_ids: list[int] | None = None
    ebook_format: str | None = None
    audiobook_format: str | None = None
    # Ids to resolve first during the size sweep that follows a refresh --
    # normally the user's current checkbox selection, so a selected book
    # isn't stuck behind a whole library's worth of others.
    selected: list[int] | None = None


class SweepRequest(BaseModel):
    selected: list[int] | None = None
    # False = cache-only sweep (resolve sizes already on disk, no litres.ru
    # calls). The frontend's automatic on-load sweep sends False so merely
    # opening the app never fires a library's worth of size requests; the
    # explicit Refresh sends the default (live).
    live: bool = True


class PrefsUpdate(BaseModel):
    # All optional: a caller pushes just the field(s) that changed (the
    # selection, or one format) without clobbering the others.
    selected: list[int] | None = None
    ebook_format: str | None = None
    audiobook_format: str | None = None
    # Folder a finished archive is saved into. "" clears it back to the
    # LITRES_DOWNLOAD_DIR default (None here means "leave alone", as above).
    download_dir: str | None = None


@app.get("/activity")
def get_activity():
    # Fold the shared UI state (selection + formats) into the poll response the
    # frontend already fetches, so every open browser converges on the same
    # ticked books and format choices -- not just the same progress.
    root = library_root_from_env()
    return {
        **activity.snapshot(),
        "prefs": prefs.snapshot(),
        "library_sync_enabled": root is not None,
        "library_dir": str(root) if root else None,
        # art_ids already sitting complete in the loose-file mirror, so each
        # card can show "you already have this" before anything is started.
        # Cache-only and a stat per book -- no litres.ru requests.
        "on_disk": activity.books_on_disk(_mirror_root()),
    }


@app.get("/prefs")
def get_prefs():
    return {"ok": True, **prefs.snapshot()}


@app.post("/prefs/browse")
def browse_download_dir():
    """Open the machine's native folder picker and store what the user chose.

    A sync def on purpose: the dialog blocks until a human answers it, so
    FastAPI runs this on the threadpool and the event loop keeps serving the
    /activity poll while the dialog is open. It deliberately does NOT go
    through session.run -- that's the single Playwright worker thread, and
    parking it behind a dialog would stall any running download.

    The picked path is validated by prefs.update() like any typed one; this
    route grants no extra reach into the filesystem."""
    if not folder_dialog.is_available():
        return JSONResponse(
            {"ok": False, "error": "No folder picker is available here -- type the path instead."},
            status_code=501,
        )
    try:
        chosen = folder_dialog.choose_folder(prefs.snapshot()["download_dir_effective"])
    except folder_dialog.DialogBusy:
        return JSONResponse(
            {"ok": False, "error": "A folder dialog is already open."}, status_code=409
        )
    except folder_dialog.FolderDialogError as exc:
        logger.warning("Folder dialog failed: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "The folder picker could not be opened."}, status_code=503
        )
    if chosen is None:
        # Cancelled -- not an error, and nothing changes. The current snapshot
        # comes back so the UI can just re-render from one shape either way.
        logger.info("Folder picker cancelled -- save folder unchanged")
        return {"ok": True, "cancelled": True, **prefs.snapshot()}
    try:
        updated = prefs.update(download_dir=chosen)
    except prefs.InvalidDownloadDir as exc:
        # The picker can reach folders the guard won't accept (e.g. /Library).
        # Same fixed-table message as the typed path -- no filesystem detail.
        logger.info("Picked folder rejected by the save-folder guard (%s)", exc.code)
        message = prefs.DOWNLOAD_DIR_ERRORS.get(exc.code, "That folder can't be used.")
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    logger.info("Save folder set from the native picker")
    return {"ok": True, "cancelled": False, **updated}


@app.post("/prefs")
def set_prefs(req: PrefsUpdate):
    try:
        updated = prefs.update(
            selected=req.selected,
            ebook_format=req.ebook_format,
            audiobook_format=req.audiobook_format,
            download_dir=req.download_dir,
        )
    except prefs.InvalidDownloadDir as exc:
        # An unusable destination folder. Reject it now, while the user is
        # looking at the field -- not at the end of a multi-gigabyte build.
        # The message is looked up from a fixed table rather than taken from
        # the exception, so no internal/filesystem detail can leak into the
        # response (CodeQL py/stack-trace-exposure).
        message = prefs.DOWNLOAD_DIR_ERRORS.get(exc.code, "That folder can't be used.")
        return JSONResponse({"ok": False, "error": message}, status_code=400)
    return {"ok": True, **updated}


@app.post("/activity/refresh-library")
def refresh_library_activity(req: SweepRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    started = activity.refresh(client, req.selected)
    return {"ok": True, "started": started}


@app.post("/activity/check-sizes")
def check_sizes_activity(req: SweepRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    started = activity.check_sizes(client, req.selected, live=req.live)
    return {"ok": True, "started": started}


class SyncRequest(BaseModel):
    audio_only: bool = True
    ebook_format: str | None = None
    audiobook_format: str | None = None
    # Optional subset; None = entire library (subject to audio_only).
    art_ids: list[int] | None = None


@app.post("/activity/sync-audiobookshelf")
def sync_audiobookshelf_activity(req: SyncRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    if library_root_from_env() is None:
        return JSONResponse(
            {
                "ok": False,
                "error": "LITRES_LIBRARY_DIR is not configured — set it to an on-disk library path.",
            },
            status_code=400,
        )
    art_ids = set(req.art_ids) if req.art_ids is not None else None
    # Prefer explicit request formats, else server prefs.
    p = prefs.snapshot()
    started = activity.start_sync(
        client,
        audio_only=req.audio_only,
        preferred_ext=req.ebook_format if req.ebook_format is not None else p.get("ebook_format"),
        preferred_file_type=req.audiobook_format
        if req.audiobook_format is not None
        else p.get("audiobook_format"),
        art_ids=art_ids,
    )
    return {"ok": True, "started": started}


@app.post("/activity/prepare-zip")
def prepare_zip_activity(req: PrepareRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    # `None` means "no filter" (prepare everything); an explicitly empty
    # list means the caller selected zero books, which is an error, not
    # "everything" -- those must not collapse into the same falsy check.
    if req.art_ids is not None and len(req.art_ids) == 0:
        return JSONResponse({"ok": False, "error": "No books selected"}, status_code=400)
    art_ids = set(req.art_ids) if req.art_ids is not None else None
    # Resolved here rather than inside activity.py, so the state machine stays
    # free of prefs -- same as the two format preferences, which the frontend
    # sends along with the request.
    started = activity.prepare(
        client, art_ids, req.ebook_format, req.audiobook_format,
        dest_dir=prefs.resolve_download_dir(),
        # Books already downloaded as loose files are packed from disk instead
        # of being fetched again -- the saving that matters is the request, not
        # the bytes.
        mirror_root=_mirror_root(),
    )
    return {"ok": True, "started": started}


@app.post("/activity/stop")
def stop_activity():
    logger.info("Cancel requested via /activity/stop")
    cancelled = activity.cancel()
    return {"ok": True, "cancelled": cancelled}


MIRROR_SUBFOLDER = "BookVault library"


def _mirror_root() -> Path | None:
    """Where the loose-file mirror lives: a subfolder of the configured save
    folder, so hundreds of book files never scatter among the user's other
    downloads. None only when there's no save folder at all (tests)."""
    root = prefs.resolve_download_dir()
    return (root / MIRROR_SUBFOLDER) if root else None


@app.post("/activity/download-files")
def download_files_activity(req: PrepareRequest):
    """Download the selected books into the save folder as loose files.

    Same transfers as /activity/prepare without the zip; a book already there
    and the right size is skipped rather than fetched again."""
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    # Same distinction prepare makes: None means "everything", an explicitly
    # empty list means the user selected nothing, which is an error.
    if req.art_ids is not None and len(req.art_ids) == 0:
        return JSONResponse({"ok": False, "error": "No books selected"}, status_code=400)
    dest = _mirror_root()
    if dest is None:
        return JSONResponse(
            {"ok": False, "error": "No save folder is configured."}, status_code=400
        )
    art_ids = set(req.art_ids) if req.art_ids is not None else None
    started = activity.download_files(
        client, art_ids, req.ebook_format, req.audiobook_format, dest_root=dest,
    )
    return {"ok": True, "started": started}


@app.post("/download/save-copy")
def save_archive_copy():
    """Pick a folder and put an extra copy of the finished archive in it.

    The build already auto-saved the archive to the configured save folder;
    this is purely additional, so the original is never moved or removed. Sync
    def (threadpool) for the same reason as /prefs/browse: the dialog waits on
    a human and must not park the Playwright worker."""
    if not folder_dialog.is_available():
        return JSONResponse(
            {"ok": False, "error": "No folder picker is available here."}, status_code=501
        )
    if not activity.snapshot().get("zip_path"):
        return JSONResponse(
            {"ok": False, "error": "There's no finished archive to copy yet."}, status_code=409
        )
    try:
        chosen = folder_dialog.choose_folder(prefs.snapshot()["download_dir_effective"])
    except folder_dialog.DialogBusy:
        return JSONResponse({"ok": False, "error": "A folder dialog is already open."}, status_code=409)
    except folder_dialog.FolderDialogError as exc:
        logger.warning("Folder dialog failed while saving a copy: %s", exc)
        return JSONResponse({"ok": False, "error": "The folder picker could not be opened."}, status_code=503)
    if chosen is None:
        return {"ok": True, "cancelled": True}

    # Same bar as the configured save folder -- absolute, a real writable
    # folder, inside the allowed roots -- but validated WITHOUT storing it, so
    # saving a copy somewhere never silently changes where builds go.
    try:
        dest = prefs.validate_download_dir(chosen)
    except prefs.InvalidDownloadDir as exc:
        logger.info("Copy destination rejected (%s)", exc.code)
        message = prefs.DOWNLOAD_DIR_ERRORS.get(exc.code, "That folder can't be used.")
        return JSONResponse({"ok": False, "error": message}, status_code=400)

    try:
        target = activity.copy_archive_to(Path(dest))
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "error": "There's no finished archive to copy yet."}, status_code=409
        )
    except OSError as exc:
        # Out of space, a drive unmounted between the pick and the copy.
        # Fixed message: the OSError's text can carry filesystem detail.
        logger.warning("Copying the archive failed: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "The copy could not be written to that folder."}, status_code=503
        )
    logger.info("Saved an extra copy of the archive")
    return {"ok": True, "cancelled": False, "copied_to": str(target)}


@app.get("/download/file")
def download_file_route():
    zip_path = activity.snapshot().get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        return RedirectResponse("/", status_code=303)
    return FileResponse(zip_path, filename="litres-library.zip", media_type="application/zip")
