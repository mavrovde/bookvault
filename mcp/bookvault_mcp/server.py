"""MCP server exposing the LitRes library as tools for MCP clients (e.g.
Claude Desktop), reusing the same session/login logic as the web UI.

Run standalone over stdio:
    .venv/bin/bookvault-mcp        # or: python -m bookvault_mcp.server

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
from bookvault_core import session
from bookvault_core.client import LitresAuthError, LitresClient
from bookvault_core.library_fs import (
    download_to_temp_then_rename,
    file_is_complete,
    library_root_from_env,
    read_mirror_index,
    record_in_mirror_index,
)
from bookvault_core.library_sync import sync_library, sync_one
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logger = logging.getLogger(__name__)

mcp = FastMCP("bookvault")

DOWNLOAD_DIR = Path(os.environ.get("LITRES_DOWNLOAD_DIR", str(Path.home() / "Downloads" / "litres-library")))

# Ceiling on how many titles list_library will return in one call. The result
# goes straight into the caller's context window, so an unbounded listing of a
# large account is a real failure mode, not a theoretical one.
MAX_LIST_LIMIT = 500


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
    """List up to `limit` purchased litres.ru items with their metadata.

    Each item is shaped by `LitresClient.normalize_library_item`: id, uuid,
    title, subtitle, authors, narrators, series, is_audio, language_code,
    cover_url, url, purchase/release dates, symbols_count, DRM and archived
    flags, and the average rating.

    Deliberately not the full API record -- storefront data (prices, labels),
    layout data (cover dimensions) and internal ids are omitted, since this
    result goes into the caller's context window. Detail-only fields such as
    ISBN, genres and the HTML annotation aren't on the listing endpoint at all.

    `limit` is clamped to 1..MAX_LIST_LIMIT.
    """
    await _ensure_logged_in()
    client = session.current_client()

    # A model can pass anything here. Clamp rather than trusting it: 0 or a
    # negative would otherwise still yield one item (the bound is checked
    # after the first append) *and* send `?limit=0` upstream, and an
    # unbounded value would page the entire library into a context window.
    wanted = max(1, min(int(limit), MAX_LIST_LIMIT))

    def _sync():
        items = []
        # The per-page size sent to litres.ru, capped so one call can't turn
        # into an enormous listing request.
        for art in client.iter_library(limit=min(wanted, 100)):
            items.append(LitresClient.normalize_library_item(art))
            if len(items) >= wanted:
                break
        return items

    return await session.run_async(_sync)


@mcp.tool()
async def download_book(art_id: int) -> dict:
    """Download one purchased book/audiobook by its art id.

    When LITRES_LIBRARY_DIR is set, installs into the ABS-compatible
    Author/Title library (with metadata.json). Otherwise saves a flat file
    under LITRES_DOWNLOAD_DIR (default ~/Downloads/litres-library).
    """
    await _ensure_logged_in()
    client = session.current_client()
    library_root = library_root_from_env()

    def _sync():
        if library_root is not None:
            # One detail request, not a walk of the whole library. The previous
            # shape paged through every purchased title looking for this id --
            # for a large account that's a run of listing requests before a
            # single byte is downloaded, which is exactly the cadence the
            # anti-bot layer notices. The listing row is only used as a
            # fallback if the detail endpoint has nothing.
            art = None
            try:
                art = client.get_art(art_id)
            except Exception as exc:  # noqa: BLE001 -- fall back to the listing below
                logger.info("Art detail lookup failed for %s, falling back to the listing: %s", art_id, exc)
            if art is None:
                art = next(
                    (item for item in client.iter_library() if item.get("id") == art_id),
                    None,
                )
            if art is None:
                logger.warning("Art %s not found in the library or via the detail endpoint", art_id)
                return {"ok": False, "error": f"Could not look up art {art_id} on litres.ru."}
            row = sync_one(client, library_root, art)
            if row.get("status") == "done":
                return {
                    "ok": True,
                    "path": row.get("path"),
                    "status": "done",
                    "layout": "library",
                }
            if row.get("status") == "skipped":
                return {
                    "ok": True,
                    "path": row.get("path"),
                    "status": "skipped",
                    "reason": row.get("reason"),
                    "layout": "library",
                }
            return {"ok": False, "error": row.get("error") or row.get("reason") or "download failed"}

        files = client.get_files(art_id)
        best = client.pick_best_file(files)
        if best is None:
            return {"ok": False, "error": f"No downloadable file for art {art_id}"}
        ext = client.file_extension(best)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = DOWNLOAD_DIR / f"{art_id}.{ext}"

        # Already here and intact? Don't fetch it again. An agent calling this
        # tool in a loop over a library would otherwise re-download everything
        # on every run -- the request volume that gets an account anti-bot
        # flagged, for no benefit.
        #
        # Judged against what a previous run recorded writing, NOT against the
        # size in litres.ru's listing: that size does not describe the bytes
        # the site serves (see record_in_mirror_index), so comparing to it
        # would make this skip fire essentially never. Same index and same rule
        # as the web app's mirror, so the two can't disagree about what
        # "already downloaded" means.
        recorded = read_mirror_index(DOWNLOAD_DIR).get(str(art_id)) or {}
        if file_is_complete(dest, recorded.get("size")):
            logger.info("Art %s already downloaded, skipping", art_id)
            return {
                "ok": True,
                "path": str(dest),
                "size_bytes": dest.stat().st_size,
                "layout": "flat",
                "status": "exists",
            }

        # A file that's present but not the length we recorded is a partial
        # download; it gets overwritten rather than trusted.
        replaced = dest.exists()
        download_to_temp_then_rename(client, art_id, best["id"], dest)
        # Record what we actually wrote, so the next call can recognise it.
        # After the rename, never before: a failed transfer must leave no
        # record claiming success.
        record_in_mirror_index(DOWNLOAD_DIR, art_id, dest.name, dest.stat().st_size)
        return {
            "ok": True,
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "layout": "flat",
            "status": "replaced" if replaced else "done",
        }

    return await session.run_async(_sync)


@mcp.tool()
async def sync_library_now(audio_only: bool = True) -> dict:
    """Sync purchased titles into LITRES_LIBRARY_DIR (ABS Author/Title layout).

    Requires LITRES_LIBRARY_DIR. Returns counts and a per-title log.
    """
    root = library_root_from_env()
    if root is None:
        return {
            "ok": False,
            "error": "LITRES_LIBRARY_DIR is not set — configure an on-disk library path first.",
        }
    await _ensure_logged_in()
    client = session.current_client()

    def _sync():
        summary = sync_library(client, root, audio_only=audio_only)
        return {"ok": True, **summary}

    return await session.run_async(_sync)


def main() -> None:
    # Logs go to stderr, not stdout -- under the stdio transport, stdout IS the
    # MCP protocol stream, and any stray log line there would corrupt it.
    logging.basicConfig(
        level=os.environ.get("LITRES_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # Default stdio: launched by an MCP client (Claude Desktop) with stdin
    # attached, incl. `docker run -i`. In the Docker Compose deployment there's
    # no attached stdin, so the container runs a network transport instead
    # (LITRES_MCP_TRANSPORT=streamable-http) -- a long-lived service Compose can
    # start/stop and an MCP client connects to over http://host:port/mcp.
    transport = os.environ.get("LITRES_MCP_TRANSPORT", "stdio").lower()
    if transport in ("http", "streamable_http", "streamable-http"):
        transport = "streamable-http"
    if transport in ("streamable-http", "sse"):
        mcp.settings.host = os.environ.get("LITRES_MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.environ.get("LITRES_MCP_PORT", "8421"))
        logger.info("Starting MCP server over %s at %s:%s", transport, mcp.settings.host, mcp.settings.port)
        mcp.run(transport=transport)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
