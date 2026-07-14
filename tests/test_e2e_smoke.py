"""End-to-end smoke tests -- offline, CI-friendly.

These exercise the *whole* stack front-to-back against a fake litres.ru
backend (no Playwright, no network), complementing the finer-grained route
tests in test_web.py / test_mcp_server.py:

  * test_web_server_process_boots_and_serves     -- the real `bookvault-web`
    entry point actually starts uvicorn and serves HTTP (catches packaging /
    boot / import regressions a TestClient can't).
  * test_web_full_backup_flow                    -- login -> list library ->
    build zip -> download, driven through the real ASGI app + activity state
    machine, ending in a valid, openable zip.
  * test_mcp_full_backup_flow                     -- the MCP tools end-to-end:
    login_status -> login -> list_library -> download_book -> file on disk.

The live counterpart (against a really-running server) lives in
test_smoke_live.py and is opt-in via `-m live`.
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from bookvault_core import session
from bookvault_mcp import server as mcp_server
from bookvault_web import activity
from bookvault_web.app import app
from tests.fakes import client_factory


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_idle(timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = activity.snapshot()
        if snap["state"] == activity.IDLE:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"activity did not settle in time: {activity.snapshot()}")


def test_web_server_process_boots_and_serves(tmp_path):
    """The real console-script path: spawn `python -m bookvault_web.run`, and
    confirm it boots uvicorn and serves the login page over real HTTP.

    Run from a temp cwd with temp session/cache files and no `.env`, so it
    starts logged-out (no saved session, no keychain, env login is MCP-only)
    -- which means no Playwright browser is ever launched."""
    port = _free_port()
    env = {
        **os.environ,
        "LITRES_APP_HOST": "127.0.0.1",
        "LITRES_APP_PORT": str(port),
        "LITRES_RELOAD": "0",  # no file-watcher subprocess
        "LITRES_LOG_LEVEL": "WARNING",
        "LITRES_SESSION_FILE": str(tmp_path / "session.json"),  # absent -> logged out
        "LITRES_CACHE_FILE": str(tmp_path / "cache.json"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "bookvault_web.run"],
        cwd=tmp_path,  # no .env here -> real credentials never load
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        url = f"http://127.0.0.1:{port}/"
        body, status = _poll_http(url, timeout=30.0)
        assert status == 200, f"unexpected status {status}"
        assert 'name="login"' in body  # the login form rendered end-to-end
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _poll_http(url: str, timeout: float) -> tuple[str, int]:
    """Poll `url` until it answers (or `timeout`). Returns (body, status)."""
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.read().decode("utf-8", "replace"), resp.status
        except urllib.error.HTTPError as exc:  # server up, non-2xx
            return exc.read().decode("utf-8", "replace"), exc.code
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_err = exc
            time.sleep(0.2)
    raise AssertionError(f"server at {url} never came up within {timeout}s (last: {last_err})")


def test_web_full_backup_flow(monkeypatch):
    """login -> browse library -> build zip -> download, through the real
    FastAPI app and the real activity state machine, ending in a valid zip."""
    library = [
        {"id": 1, "title": "Book One", "art_type": 0, "persons": [], "cover_url": None},
        {"id": 2, "title": "Book Two", "art_type": 0, "persons": [], "cover_url": None},
    ]
    files_by_id = {
        1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 8}],
        2: [{"id": 200, "extension": "epub", "is_additional": False, "size": 8}],
    }
    client_factory(monkeypatch, session, library=library, files_by_id=files_by_id)

    with TestClient(app) as client:
        # 1. log in
        login = client.post("/login", data={"login": "user@example.com", "password": "pw"})
        assert login.status_code == 200
        assert session.current_login() == "user@example.com"

        # 2. the library lists both books
        lib = client.get("/library")
        assert lib.status_code == 200
        assert [b["id"] for b in lib.json()["books"]] == [1, 2]

        # 3. build a zip of everything
        prep = client.post("/activity/prepare", json={"art_ids": [1, 2]})
        assert prep.status_code == 200 and prep.json()["started"] is True
        snap = _wait_until_idle()
        assert snap["error"] is None, snap
        assert snap["zip_path"], "zip was not produced"

        # 4. download it and confirm it's a real, openable zip with both books
        dl = client.get("/download/file")
        assert dl.status_code == 200
        assert dl.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
            assert zf.testzip() is None  # no corrupt members
            assert len(zf.namelist()) == 2  # one file per selected book


async def test_mcp_full_backup_flow(monkeypatch, tmp_path):
    """The MCP surface end-to-end: not-logged-in -> login -> list -> download."""
    monkeypatch.setattr(mcp_server, "DOWNLOAD_DIR", tmp_path / "out")
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}, {"id": 2, "title": "Book Two"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 8}]},
    )

    # starts logged out
    assert await mcp_server.login_status() == {"logged_in": False, "login": None}

    # log in, then the session is live
    assert await mcp_server.login_to_litres("user@example.com", "pw") == {
        "ok": True,
        "login": "user@example.com",
    }
    assert (await mcp_server.login_status())["logged_in"] is True

    # list the library
    items = await mcp_server.list_library()
    assert [b["id"] for b in items] == [1, 2]

    # download one, and the file lands on disk
    result = await mcp_server.download_book(1)
    assert result["ok"] is True
    assert Path(result["path"]).exists()
    assert Path(result["path"]).read_bytes() == b"FAKEDATA"
