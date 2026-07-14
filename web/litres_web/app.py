"""Minimal local, single-user web app: log in once, click a button, get your
whole litres.ru library as a zip.

Intentionally bound to 127.0.0.1 only (see run.py) -- this is a personal
tool for the account owner, not a multi-user service.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import List, Optional

import anyio
from fastapi import FastAPI, Form
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from litres_core import cache, session
from litres_core.client import AUDIOBOOK_FILE_TYPES, EBOOK_EXTENSIONS, LitresAuthError

from . import activity

logger = logging.getLogger(__name__)

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
    await anyio.to_thread.run_sync(partial(session.restore_session, allow_env_login=False))
    yield
    await anyio.to_thread.run_sync(session.shutdown)


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "logged_in": session.current_client() is not None,
            "login": session.current_login(),
            "error": None,
            "ebook_formats": EBOOK_EXTENSIONS,
            "audiobook_formats": AUDIOBOOK_FILE_TYPES,
        },
    )


@app.post("/login")
def do_login(request: Request, login: str = Form(...), password: str = Form(...)):
    try:
        session.login(login, password)
    except LitresAuthError as exc:
        logger.warning("Login attempt failed for %s: %s", login, exc)
        return templates.TemplateResponse(
            request,
            "index.html",
            {"logged_in": False, "login": None, "error": str(exc)},
            status_code=401,
        )
    logger.info("Login attempt succeeded for %s", login)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def do_logout():
    logger.info("Logout requested for %s", session.current_login())
    session.logout()
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
    except Exception as exc:
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
    # CHECKING activity (litres_web/activity.py), which sweeps sizes in the
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
    except Exception as exc:
        # Best-effort -- a failed size fetch just leaves that book's size
        # unknown; a clean error here is enough, no need to retry serverside.
        logger.info("Size fetch failed for art %s: %s", art_id, exc)
        return JSONResponse({"ok": False, "error": "Could not fetch size"}, status_code=503)
    cache.set_files(art_id, files)
    return {"ok": True, "size_mb": size_mb, "cached": False}


# --------------------------------------------------------------------------
# Activity: the single backend state machine (see litres_web/activity.py). The UI
# starts an activity via one of the POST routes below and then polls
# GET /activity to render whatever state it reports -- it owns no
# activity/progress logic of its own.
# --------------------------------------------------------------------------


class PrepareRequest(BaseModel):
    art_ids: Optional[List[int]] = None
    ebook_format: Optional[str] = None
    audiobook_format: Optional[str] = None
    # Ids to resolve first during the size sweep that follows a refresh --
    # normally the user's current checkbox selection, so a selected book
    # isn't stuck behind a whole library's worth of others.
    selected: Optional[List[int]] = None


class SweepRequest(BaseModel):
    selected: Optional[List[int]] = None


@app.get("/activity")
def get_activity():
    return activity.snapshot()


@app.post("/activity/refresh")
def refresh_activity(req: SweepRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    started = activity.refresh(client, req.selected)
    return {"ok": True, "started": started}


@app.post("/activity/check")
def check_activity(req: SweepRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    started = activity.check_sizes(client, req.selected)
    return {"ok": True, "started": started}


@app.post("/activity/prepare")
def prepare_activity(req: PrepareRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    # `None` means "no filter" (prepare everything); an explicitly empty
    # list means the caller selected zero books, which is an error, not
    # "everything" -- those must not collapse into the same falsy check.
    if req.art_ids is not None and len(req.art_ids) == 0:
        return JSONResponse({"ok": False, "error": "No books selected"}, status_code=400)
    art_ids = set(req.art_ids) if req.art_ids is not None else None
    started = activity.prepare(client, art_ids, req.ebook_format, req.audiobook_format)
    return {"ok": True, "started": started}


@app.post("/activity/cancel")
def cancel_activity():
    logger.info("Cancel requested via /activity/cancel")
    cancelled = activity.cancel()
    return {"ok": True, "cancelled": cancelled}


@app.get("/download/file")
def download_file_route():
    zip_path = activity.snapshot().get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        return RedirectResponse("/", status_code=303)
    return FileResponse(zip_path, filename="litres-library.zip", media_type="application/zip")
