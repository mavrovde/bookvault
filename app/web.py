"""Minimal local, single-user web app: log in once, click a button, get your
whole litres.ru library as a zip.

Intentionally bound to 127.0.0.1 only (see run.py) -- this is a personal
tool for the account owner, not a multi-user service.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import anyio
from fastapi import FastAPI, Form
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import download_job, session
from .client import AUDIOBOOK_FILE_TYPES, EBOOK_EXTENSIONS, LitresAuthError

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sync Playwright refuses to run inside an asyncio loop, and FastAPI's
    # lifespan runs directly on the event loop thread -- push it to a
    # worker thread, same as Starlette does for sync route handlers.
    await anyio.to_thread.run_sync(session.restore_session)
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


def _list_books(client):
    books = []
    for art in client.iter_library():
        authors = [p.get("full_name") for p in (art.get("persons") or []) if p.get("role") == "author"]
        cover_url = art.get("cover_url")
        books.append(
            {
                "id": art.get("id"),
                "title": art.get("title") or str(art.get("id")),
                "authors": ", ".join(a for a in authors if a),
                "is_audio": art.get("art_type") == 1,
                "cover_url": f"https://static.litres.ru{cover_url}" if cover_url else None,
            }
        )
    return books


@app.get("/library")
def get_library():
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    books = session.run(_list_books, client)
    return {"ok": True, "books": books}


def _book_size_mb(client, art_id):
    files = client.get_files(art_id)
    best = client.pick_best_file(files)
    size = best.get("size") if best else None
    return round(size / 1e6, 1) if size else None


@app.get("/library/{art_id}/size")
def get_book_size(art_id: int):
    # Deliberately not part of /library: fetching every book's file size
    # upfront would mean one extra API call per book (this backend has a
    # single dedicated worker thread -- see session.py -- so that's fully
    # sequential, not parallel). The UI fetches this lazily per row instead
    # so the initial library list stays fast.
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    size_mb = session.run(_book_size_mb, client, art_id)
    return {"ok": True, "size_mb": size_mb}


class DownloadRequest(BaseModel):
    art_ids: Optional[List[int]] = None
    ebook_format: Optional[str] = None
    audiobook_format: Optional[str] = None


@app.post("/download/start")
def start_download(req: DownloadRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    # `None` means "no filter" (download everything); an explicitly empty
    # list means the caller selected zero books, which is an error, not
    # "everything" -- those must not collapse into the same falsy check.
    if req.art_ids is not None and len(req.art_ids) == 0:
        return JSONResponse({"ok": False, "error": "No books selected"}, status_code=400)
    art_ids = set(req.art_ids) if req.art_ids is not None else None
    logger.info(
        "Download requested: %s, ebook_format=%s, audiobook_format=%s",
        f"{len(art_ids)} book(s)" if art_ids is not None else "entire library",
        req.ebook_format, req.audiobook_format,
    )
    started = download_job.start(client, art_ids, req.ebook_format, req.audiobook_format)
    return {"ok": True, "started": started}


@app.post("/download/cancel")
def cancel_download():
    logger.info("Cancel requested via /download/cancel")
    cancelled = download_job.cancel()
    return {"ok": True, "cancelled": cancelled}


@app.get("/download/status")
def download_status():
    return download_job.snapshot()


@app.get("/download/file")
def download_file_route():
    zip_path = download_job.snapshot().get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        return RedirectResponse("/", status_code=303)
    return FileResponse(zip_path, filename="litres-library.zip", media_type="application/zip")
