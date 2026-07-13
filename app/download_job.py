"""Background job that downloads the whole library into a zip, exposing
progress state the web UI can poll (see /download/status in web.py).

The job runs via `session.submit` (the one dedicated Playwright thread --
see session.py), not a plain new `threading.Thread`: Playwright's sync API
only works on the exact thread that created it, so a raw new thread would
fail with "Cannot switch to a different thread" the moment it touched the
client. Submitting to the dedicated thread also means /download/start
returns immediately and the browser polls /download/status instead of
blocking on one long request.
"""
from __future__ import annotations

import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Optional

from . import session
from .client import LitresClient

_lock = threading.Lock()
_state = {
    "status": "idle",  # idle | running | done | error
    "current_title": None,
    "done": 0,
    "total": None,  # known when a specific selection was requested
    "log": [],  # [{"title", "ext"?, "size_mb"?, "status": "done"|"skipped"}]
    "error": None,
    "zip_path": None,
}


def snapshot() -> dict:
    with _lock:
        return {**_state, "log": list(_state["log"])}


def start(
    client: LitresClient,
    art_ids: Optional[set] = None,
    preferred_ext: Optional[str] = None,
    preferred_file_type: Optional[str] = None,
) -> bool:
    """Kick off a download job in the background. Returns False if one is
    already running (so the caller can no-op instead of starting a second).

    `art_ids`, if given, restricts the zip to just those book ids (the
    checkbox selection in the UI) -- None/empty means "everything".
    `preferred_ext`/`preferred_file_type` are the user's chosen default
    ebook/audiobook formats, falling back to the built-in preference order
    per book when unavailable."""
    with _lock:
        if _state["status"] == "running":
            return False
        _state.update(
            status="running",
            current_title=None,
            done=0,
            total=len(art_ids) if art_ids else None,
            log=[],
            error=None,
            zip_path=None,
        )

    session.submit(_run, client, art_ids, preferred_ext, preferred_file_type)
    return True


def _run(
    client: LitresClient,
    art_ids: Optional[set],
    preferred_ext: Optional[str],
    preferred_file_type: Optional[str],
) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="litres-"))
    zip_path = workdir / "litres-library.zip"
    try:
        # Audiobook bundles/text formats are already compressed -- STORED
        # avoids burning CPU re-deflating gigabytes of mp3/epub for no size
        # benefit.
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for art in client.iter_library():
                art_id = art.get("id")
                if art_ids and art_id not in art_ids:
                    continue
                title = art.get("title") or str(art_id)
                with _lock:
                    _state["current_title"] = title

                files = client.get_files(art_id)
                best = client.pick_best_file(files, preferred_ext, preferred_file_type)
                if best is None:
                    with _lock:
                        _state["log"].append({"title": title, "status": "skipped"})
                    continue

                ext = client.file_extension(best)
                size_mb = round(best.get("size", 0) / 1e6, 1)
                safe_title = "".join(c for c in title if c.isalnum() or c in " ._-")[:150]
                dest = workdir / f"{safe_title}.{ext}"
                client.download_file(art_id, best["id"], dest.name, dest)
                zf.write(dest, arcname=dest.name)
                dest.unlink()

                with _lock:
                    _state["done"] += 1
                    _state["log"].append(
                        {"title": title, "ext": ext, "size_mb": size_mb, "status": "done"}
                    )
        with _lock:
            _state["status"] = "done"
            _state["current_title"] = None
            _state["zip_path"] = str(zip_path)
    except Exception as exc:
        with _lock:
            _state["status"] = "error"
            _state["error"] = str(exc)
            _state["current_title"] = None
