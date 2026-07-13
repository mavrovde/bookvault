"""Minimal local, single-user web app: log in once, click a button, get your
whole litres.ru library as a zip.

Intentionally bound to 127.0.0.1 only (see run.py) -- this is a personal
tool for the account owner, not a multi-user service.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import anyio
from fastapi import FastAPI, Form
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import download_job, session
from .client import AUDIOBOOK_FILE_TYPES, EBOOK_EXTENSIONS, LitresAuthError

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
        return templates.TemplateResponse(
            request,
            "index.html",
            {"logged_in": False, "login": None, "error": str(exc)},
            status_code=401,
        )
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def do_logout():
    session.logout()
    return RedirectResponse("/", status_code=303)


def _list_books(client):
    return [
        {"id": art.get("id"), "title": art.get("title") or str(art.get("id"))}
        for art in client.iter_library()
    ]


@app.get("/library")
def get_library():
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    books = session.run(_list_books, client)
    return {"ok": True, "books": books}


class DownloadRequest(BaseModel):
    art_ids: Optional[List[int]] = None
    ebook_format: Optional[str] = None
    audiobook_format: Optional[str] = None


@app.post("/download/start")
def start_download(req: DownloadRequest):
    client = session.current_client()
    if client is None:
        return JSONResponse({"ok": False, "error": "Not logged in"}, status_code=401)
    art_ids = set(req.art_ids) if req.art_ids else None
    started = download_job.start(client, art_ids, req.ebook_format, req.audiobook_format)
    return {"ok": True, "started": started}


@app.get("/download/status")
def download_status():
    return download_job.snapshot()


@app.get("/download/file")
def download_file_route():
    zip_path = download_job.snapshot().get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        return RedirectResponse("/", status_code=303)
    return FileResponse(zip_path, filename="litres-library.zip", media_type="application/zip")
