"""Tests for bookvault_mcp/server.py. Tool functions are plain async callables
even after @mcp.tool() (FastMCP registers them without wrapping), so they
can be awaited directly -- no need to spin up a real MCP stdio client for
unit-level coverage."""
from __future__ import annotations

import inspect
import logging
import sys

import pytest
from bookvault_core import credentials, session
from bookvault_core.client import LitresAuthError
from bookvault_mcp import server as mcp_server

from tests.fakes import client_factory


def _logged_in(monkeypatch, **kwargs):
    """A restorable session + a fake client. `_ensure_logged_in` restores from
    the keychain, so the credentials have to exist before the tool is called."""
    credentials.save("user@example.com", "hunter2")
    return client_factory(monkeypatch, session, **kwargs)


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
    assert result["layout"] == "flat"


async def test_download_book_into_library_dir(monkeypatch, tmp_path):
    lib = tmp_path / "abs-lib"
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(lib))
    credentials.save("user@example.com", "hunter2")
    client_factory(
        monkeypatch,
        session,
        library=[
            {
                "id": 1,
                "title": "Book One",
                "art_type": 1,
                "persons": [{"full_name": "Author A", "role": "author"}],
                "last_released_at": "2024-01-01",
            }
        ],
        files_by_id={
            1: [
                {
                    "id": 100,
                    "extension": "m4b",
                    "file_type": "mobile_version_mp4",
                    "is_additional": False,
                    "size": 8,
                }
            ]
        },
    )

    result = await mcp_server.download_book(1)

    assert result["ok"] is True
    assert result["layout"] == "library"
    assert (lib / "Author A" / "Book One" / "metadata.json").exists()


async def test_download_book_looks_the_art_up_directly_not_by_walking_the_library(
    monkeypatch, tmp_path
):
    """One detail request, not a page-by-page scan of every purchased title.
    On a large account the scan is a run of listing requests before a single
    byte is downloaded -- exactly the cadence the anti-bot layer notices."""
    lib = tmp_path / "abs-lib"
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(lib))
    credentials.save("user@example.com", "hunter2")
    art = {
        "id": 1,
        "title": "Book One",
        "art_type": 1,
        "persons": [{"full_name": "Author A", "role": "author"}],
        "last_released_at": "2024-01-01",
    }
    fake = client_factory(
        monkeypatch,
        session,
        library=[art],
        files_by_id={
            1: [{"id": 100, "extension": "m4b", "file_type": "mobile_version_mp4",
                 "is_additional": False, "size": 8}]
        },
    )
    calls = []
    original = fake.iter_library

    def counting_iter_library(limit=100):
        calls.append(limit)
        return original(limit)

    fake.iter_library = counting_iter_library

    result = await mcp_server.download_book(1)

    assert result["ok"] is True
    assert fake.get_art_calls[0] == 1  # resolved via the detail endpoint...
    assert calls == []                 # ...without listing the library at all


async def test_sync_library_now(monkeypatch, tmp_path):
    lib = tmp_path / "abs-lib"
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(lib))
    credentials.save("user@example.com", "hunter2")
    client_factory(
        monkeypatch,
        session,
        library=[
            {
                "id": 1,
                "title": "Book One",
                "art_type": 1,
                "persons": [{"full_name": "Author A", "role": "author"}],
                "last_released_at": "2024-01-01",
            }
        ],
        files_by_id={
            1: [
                {
                    "id": 100,
                    "extension": "m4b",
                    "file_type": "mobile_version_mp4",
                    "is_additional": False,
                    "size": 8,
                }
            ]
        },
    )
    result = await mcp_server.sync_library_now(audio_only=True)
    assert result["ok"] is True
    assert result["done"] == 1


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


# ==========================================================================
# The tool surface itself. An MCP client and the model behind it see only
# the registry: names, argument schemas and descriptions. Those *are* the
# API, so they're worth asserting as much as the behaviour behind them.
# ==========================================================================


async def test_every_tool_is_registered_with_a_description():
    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
    assert set(tools) == {
        "login_status",
        "login_to_litres",
        "list_library",
        "download_book",
        "sync_library_now",
    }
    for name, tool in tools.items():
        # The description is the model's only guidance on when to call it.
        assert (tool.description or "").strip(), f"{name} has no description"


async def test_tool_argument_schemas():
    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}

    def schema(name):
        s = tools[name].inputSchema or {}
        return sorted(s.get("properties", {})), sorted(s.get("required", []))

    assert schema("login_status") == ([], [])
    assert schema("login_to_litres") == (["login", "password"], ["login", "password"])
    # limit/audio_only are optional -- a model can call these with no args.
    assert schema("list_library") == (["limit"], [])
    assert schema("download_book") == (["art_id"], ["art_id"])
    assert schema("sync_library_now") == (["audio_only"], [])


# ==========================================================================
# list_library: the payload lands in a context window, so its size and
# shape are load-bearing.
# ==========================================================================


def _library(n):
    return [
        {
            "id": i,
            "title": f"Book {i}",
            "art_type": i % 2,
            "persons": [
                {"full_name": f"Author {i}", "role": "author"},
                {"full_name": f"Reader {i}", "role": "reader"},
                {"full_name": f"Editor {i}", "role": "editor"},
            ],
            "cover_url": f"/pub/c/{i}.jpg",
            "url": f"/book/b-{i}/",
            "purchased_at": "2026-01-01T00:00:00",
            "series": [{"id": 1, "name": "Saga", "art_order": i}],
        }
        for i in range(1, n + 1)
    ]


@pytest.mark.parametrize(
    "requested,expected",
    [(3, 3), (1, 1), (10, 6), (0, 1), (-5, 1)],
)
async def test_list_library_clamps_the_limit(monkeypatch, requested, expected):
    """A model can pass anything. 0 and negatives must not silently yield a
    lone item off the back of a bound checked after the first append."""
    _logged_in(monkeypatch, library=_library(6))
    items = await mcp_server.list_library(limit=requested)
    assert len(items) == expected


async def test_list_library_never_returns_more_than_the_ceiling(monkeypatch):
    """An unbounded listing of a large account would blow the caller's
    context window -- that's a real failure mode, not a theoretical one."""
    _logged_in(monkeypatch, library=_library(mcp_server.MAX_LIST_LIMIT + 25))
    items = await mcp_server.list_library(limit=10_000)
    assert len(items) == mcp_server.MAX_LIST_LIMIT


async def test_list_library_caps_the_page_size_it_asks_litres_for(monkeypatch):
    """`limit` on iter_library is the per-page parameter sent upstream, not a
    cap on results -- a huge value would become one enormous listing call."""
    fake = _logged_in(monkeypatch, library=_library(3))
    seen = []
    original = fake.iter_library

    def recording(limit=100):
        seen.append(limit)
        return original(limit)

    fake.iter_library = recording
    await mcp_server.list_library(limit=10_000)
    assert seen == [100]


async def test_list_library_item_shape(monkeypatch):
    _logged_in(monkeypatch, library=_library(1))
    item = (await mcp_server.list_library(limit=1))[0]

    assert item["id"] == 1
    assert item["title"] == "Book 1"
    assert item["authors"] == ["Author 1"]
    assert item["narrators"] == ["Reader 1"]
    assert item["is_audio"] is True                     # art_type 1
    assert item["series"] == {"name": "Saga", "art_order": 1}
    assert item["cover_url"].startswith("https://static.litres.ru/")
    assert item["url"].startswith("https://www.litres.ru/")


async def test_list_library_omits_storefront_and_layout_fields(monkeypatch):
    """Guards the payload against quietly regrowing: every key here costs
    tokens on every call, for every title."""
    library = _library(1)
    library[0].update(
        prices={"final_price": 99.0},
        labels={"is_bestseller": True},
        cover_width=100,
        cover_height=200,
        release_file_id=555,
    )
    _logged_in(monkeypatch, library=library)
    item = (await mcp_server.list_library(limit=1))[0]
    for dropped in ("prices", "labels", "cover_width", "cover_height", "release_file_id"):
        assert dropped not in item


# ==========================================================================
# download_book / sync_library_now error paths
# ==========================================================================


async def test_download_book_does_not_leak_exception_text_to_the_client(monkeypatch, tmp_path):
    """Same rule as the web routes: a tool result is attacker-visible output.
    Echoing str(exc) leaks internal/filesystem detail (CodeQL
    py/stack-trace-exposure)."""
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    fake = _logged_in(monkeypatch, library=[])

    def boom(art_id, should_cancel=None):
        raise RuntimeError("/secret/internal/path exploded")

    fake.get_art = boom

    result = await mcp_server.download_book(4242)

    assert result["ok"] is False
    assert "secret" not in result["error"]
    assert "4242" in result["error"]


async def test_sync_library_now_refuses_without_a_library_dir(monkeypatch):
    monkeypatch.delenv("LITRES_LIBRARY_DIR", raising=False)
    result = await mcp_server.sync_library_now()
    assert result["ok"] is False
    assert "LITRES_LIBRARY_DIR" in result["error"]


async def test_sync_library_now_requires_login(monkeypatch, tmp_path):
    """The library-dir check comes first, but a configured dir must still not
    let an unauthenticated call through."""
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    with pytest.raises(RuntimeError, match="Not logged in"):
        await mcp_server.sync_library_now()


async def test_sync_library_now_passes_audio_only_through(monkeypatch, tmp_path):
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    _logged_in(monkeypatch, library=[])
    seen = {}

    def fake_sync(client, root, **kwargs):
        seen.update(kwargs)
        return {"done": 0, "skipped": 0, "failed": 0, "log": []}

    monkeypatch.setattr(mcp_server, "sync_library", fake_sync)
    await mcp_server.sync_library_now(audio_only=False)
    assert seen["audio_only"] is False


# ==========================================================================
# Transport selection in main(). The stdio default is what an MCP client
# launches; the container runs a network transport instead.
# ==========================================================================


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "stdio"),
        ("stdio", "stdio"),
        ("http", "streamable-http"),
        ("streamable_http", "streamable-http"),
        ("streamable-http", "streamable-http"),
        ("STREAMABLE-HTTP", "streamable-http"),
    ],
)
def test_main_selects_the_right_transport(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("LITRES_MCP_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("LITRES_MCP_TRANSPORT", value)
    calls = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: calls.append(kw))
    mcp_server.main()
    # stdio is mcp.run() with no kwargs; the network transports pass it through.
    assert calls == [{}] if expected == "stdio" else calls == [{"transport": expected}]


def test_main_binds_the_configured_host_and_port(monkeypatch):
    monkeypatch.setenv("LITRES_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("LITRES_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("LITRES_MCP_PORT", "9999")
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: None)
    mcp_server.main()
    assert mcp_server.mcp.settings.host == "0.0.0.0"
    assert mcp_server.mcp.settings.port == 9999


def test_main_logs_to_stderr_not_stdout(monkeypatch):
    """Under stdio, stdout IS the MCP protocol stream -- a stray log line
    there corrupts it."""
    monkeypatch.delenv("LITRES_MCP_TRANSPORT", raising=False)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kw: None)
    mcp_server.main()
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, "stream", None)
        if stream is not None:
            assert stream is not sys.stdout


async def test_download_book_reports_a_skip_on_an_unchanged_title(monkeypatch, tmp_path):
    """Idempotency is the point of the on-disk layout: a second call for an
    unchanged title must report a skip, not re-download it."""
    lib = tmp_path / "abs-lib"
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(lib))
    _logged_in(
        monkeypatch,
        library=[
            {
                "id": 1,
                "title": "Book One",
                "art_type": 1,
                "persons": [{"full_name": "Author A", "role": "author"}],
                "last_released_at": "2024-01-01",
            }
        ],
        files_by_id={
            1: [{"id": 100, "extension": "m4b", "file_type": "mobile_version_mp4",
                 "is_additional": False, "size": 8}]
        },
    )

    first = await mcp_server.download_book(1)
    second = await mcp_server.download_book(1)

    assert first["status"] == "done"
    assert second["ok"] is True
    assert second["status"] == "skipped"
    assert second["path"] == first["path"]


async def test_download_book_reports_a_library_install_failure(monkeypatch, tmp_path):
    """A failed install must surface as ok=False rather than a bare success."""
    monkeypatch.setenv("LITRES_LIBRARY_DIR", str(tmp_path / "lib"))
    _logged_in(monkeypatch, library=[{"id": 1, "title": "Book One", "art_type": 0}])
    monkeypatch.setattr(
        mcp_server,
        "sync_one",
        lambda *a, **k: {"status": "failed", "error": "disk full", "title": "Book One"},
    )

    result = await mcp_server.download_book(1)

    assert result["ok"] is False
    assert result["error"] == "disk full"
