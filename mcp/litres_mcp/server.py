"""MCP server exposing the LitRes library as tools for MCP clients (e.g.
Claude Desktop), reusing the same session/login logic as the web UI.

Run standalone over stdio:
    .venv/bin/litres-mcp        # or: python -m litres_mcp.server

Threading note: `session.restore_session`/`login`/`logout` already submit
their work to session.py's single dedicated Playwright thread internally
(see session.py's docstring for why). Tools that call raw LitresClient
methods (list_library, download_book) must submit *their* work to that same
thread via `session.run_async` -- but must do so as a separate top-level
submission, never from code that's already running inside another
submission to that same single-worker executor, or it deadlocks (the one
worker thread would be waiting on itself). Hence `_ensure_logged_in()` runs
on anyio's own worker-thread pool (a different pool), strictly before the
`session.run_async(...)` call that does the actual client work.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import anyio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from litres_core import session
from litres_core.client import LitresAuthError

load_dotenv()

mcp = FastMCP("litres-assistant")

DOWNLOAD_DIR = Path(os.environ.get("LITRES_DOWNLOAD_DIR", str(Path.home() / "Downloads" / "litres-library")))


async def _ensure_logged_in() -> None:
    if session.current_client() is None:
        await anyio.to_thread.run_sync(session.restore_session)
    if session.current_client() is None:
        raise RuntimeError(
            "Not logged in to litres.ru. Call login_to_litres(login, password) "
            "first, or set LITRES_LOGIN/LITRES_PASSWORD in .env."
        )


@mcp.tool()
async def login_status() -> dict:
    """Report whether there's an active, working litres.ru session."""

    def _sync():
        client = session.current_client()
        if client is None:
            session.restore_session()
            client = session.current_client()
        return {"logged_in": client is not None, "login": session.current_login()}

    return await anyio.to_thread.run_sync(_sync)


@mcp.tool()
async def login_to_litres(login: str, password: str) -> dict:
    """Log into litres.ru and persist the session (cookies + keychain) for future calls."""

    def _sync():
        try:
            session.login(login, password)
        except LitresAuthError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "login": login}

    return await anyio.to_thread.run_sync(_sync)


@mcp.tool()
async def list_library(limit: int = 50) -> list:
    """List up to `limit` items from the logged-in user's purchased litres.ru library."""
    await _ensure_logged_in()
    client = session.current_client()

    def _sync():
        items = []
        for art in client.iter_library(limit=limit):
            items.append({"id": art.get("id"), "title": art.get("title")})
            if len(items) >= limit:
                break
        return items

    return await session.run_async(_sync)


@mcp.tool()
async def download_book(art_id: int) -> dict:
    """Download one purchased book/audiobook by its art id to a local
    folder (~/Downloads/litres-library), returning the saved file path."""
    await _ensure_logged_in()
    client = session.current_client()

    def _sync():
        files = client.get_files(art_id)
        best = client.pick_best_file(files)
        if best is None:
            return {"ok": False, "error": f"No downloadable file for art {art_id}"}
        ext = client.file_extension(best)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = DOWNLOAD_DIR / f"{art_id}.{ext}"
        client.download_file(art_id, best["id"], dest.name, dest)
        return {"ok": True, "path": str(dest), "size_bytes": dest.stat().st_size}

    return await session.run_async(_sync)


def main() -> None:
    # Logs go to stderr, not stdout -- stdout is the MCP stdio transport
    # itself, and any stray log line there would corrupt the protocol stream.
    logging.basicConfig(
        level=os.environ.get("LITRES_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
