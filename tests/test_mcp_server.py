"""Tests for litres_mcp/server.py. Tool functions are plain async callables
even after @mcp.tool() (FastMCP registers them without wrapping), so they
can be awaited directly -- no need to spin up a real MCP stdio client for
unit-level coverage."""
from __future__ import annotations

import inspect

import pytest

from litres_core import credentials, session
from litres_mcp import server as mcp_server
from litres_core.client import LitresAuthError
from tests.fakes import client_factory


async def test_login_status_when_nothing_restorable():
    result = await mcp_server.login_status()
    assert result == {"logged_in": False, "login": None}


async def test_login_status_when_session_is_restorable(monkeypatch):
    credentials.save("user@example.com", "hunter2")
    client_factory(monkeypatch, session)

    result = await mcp_server.login_status()

    assert result == {"logged_in": True, "login": "user@example.com"}


async def test_login_to_litres_success(monkeypatch):
    client_factory(monkeypatch, session)
    result = await mcp_server.login_to_litres("user@example.com", "hunter2")
    assert result == {"ok": True, "login": "user@example.com"}
    assert session.current_login() == "user@example.com"


async def test_login_to_litres_failure(monkeypatch):
    fake = client_factory(monkeypatch, session)
    fake.fail_login = True
    result = await mcp_server.login_to_litres("user@example.com", "wrongpass")
    assert result["ok"] is False
    assert "Login failed" in result["error"]


async def test_list_library_raises_when_not_logged_in_and_nothing_to_restore():
    with pytest.raises(RuntimeError, match="Not logged in"):
        await mcp_server.list_library()


async def test_list_library_bootstraps_via_restore_session(monkeypatch):
    credentials.save("user@example.com", "hunter2")
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}, {"id": 2, "title": "Book Two"}],
    )

    items = await mcp_server.list_library()

    assert items == [{"id": 1, "title": "Book One"}, {"id": 2, "title": "Book Two"}]


async def test_list_library_respects_limit(monkeypatch):
    credentials.save("user@example.com", "hunter2")
    client_factory(
        monkeypatch,
        session,
        library=[{"id": i, "title": f"Book {i}"} for i in range(10)],
    )

    items = await mcp_server.list_library(limit=3)

    assert len(items) == 3


async def test_download_book_success(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "DOWNLOAD_DIR", tmp_path / "litres-library")
    credentials.save("user@example.com", "hunter2")
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 8}]},
    )

    result = await mcp_server.download_book(1)

    assert result["ok"] is True
    assert result["path"] == str(tmp_path / "litres-library" / "1.epub")
    assert (tmp_path / "litres-library" / "1.epub").read_bytes() == b"FAKEDATA"
    assert result["size_bytes"] == len(b"FAKEDATA")


async def test_download_book_with_no_downloadable_file(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "DOWNLOAD_DIR", tmp_path / "litres-library")
    credentials.save("user@example.com", "hunter2")
    client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book One"}], files_by_id={1: []})

    result = await mcp_server.download_book(1)

    assert result == {"ok": False, "error": "No downloadable file for art 1"}


async def test_download_book_raises_when_not_logged_in():
    with pytest.raises(RuntimeError, match="Not logged in"):
        await mcp_server.download_book(1)


async def test_download_book_propagates_download_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "DOWNLOAD_DIR", tmp_path / "litres-library")
    credentials.save("user@example.com", "hunter2")
    fake = client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 8}]},
    )
    fake.fail_downloads = {1}

    with pytest.raises(LitresAuthError):
        await mcp_server.download_book(1)


def test_ensure_logged_in_never_calls_session_run_directly():
    """Regression guard for the exact deadlock this function was written to
    avoid: session.restore_session/login already submit work to session.py's
    single-worker executor internally. If _ensure_logged_in called
    session.run/run_async *itself* (instead of going through anyio's
    separate thread pool first), any tool that awaits it before also
    awaiting session.run_async would deadlock the one shared worker thread
    against itself. This won't catch every possible regression, but it
    catches the most direct one: reintroducing a call to session.run/
    run_async inside this function."""
    source = inspect.getsource(mcp_server._ensure_logged_in)
    assert "session.run(" not in source
    assert "session.run_async(" not in source
