"""Tests for the FastAPI routes in bookvault_web/app.py, using FastAPI's TestClient
(which drives the real lifespan startup/shutdown) against a monkeypatched
LitresClient -- no real Playwright/network involved."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from bookvault_core import credentials, session
from bookvault_web import activity
from bookvault_web.app import app
from tests.fakes import client_factory


def _wait_until_idle(timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if activity.snapshot()["state"] == activity.IDLE:
            return activity.snapshot()
        time.sleep(0.005)
    raise AssertionError("activity did not settle in time")


def test_index_shows_login_form_when_not_logged_in():
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert 'name="login"' in resp.text  # the login form is present
    assert 'class="session-chip"' not in resp.text  # the logged-in chip is not


def test_index_shows_logged_in_view_after_session_restore(monkeypatch):
    credentials.save("user@example.com", "hunter2")
    client_factory(monkeypatch, session)

    with TestClient(app) as client:
        resp = client.get("/")

    assert resp.status_code == 200
    assert 'class="session-chip"' in resp.text
    assert "user@example.com" in resp.text


def test_web_app_does_not_auto_login_from_env_credentials(monkeypatch):
    """Wiring check: the lifespan restores with allow_env_login=False, so even
    with .env credentials present (and no saved session/keychain) the web app
    stays logged out and shows its login form -- env creds are MCP-only."""
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    monkeypatch.setenv("LITRES_PASSWORD", "envpass")
    fake = client_factory(monkeypatch, session, library=[])

    with TestClient(app) as client:
        resp = client.get("/")

    assert session.current_client() is None
    assert fake.login_calls == []  # never bootstrapped from .env
    assert 'name="login"' in resp.text  # login form is shown instead


def test_login_success_redirects_home(monkeypatch):
    client_factory(monkeypatch, session)
    with TestClient(app) as client:
        resp = client.post("/login", data={"login": "user@example.com", "password": "hunter2"})
    assert resp.status_code == 200  # TestClient follows the 303 redirect by default
    assert resp.url.path == "/"
    assert session.current_login() == "user@example.com"


def test_login_failure_shows_error(monkeypatch):
    fake = client_factory(monkeypatch, session)
    fake.fail_login = True
    with TestClient(app) as client:
        resp = client.post(
            "/login", data={"login": "user@example.com", "password": "wrong"}, follow_redirects=False
        )
    assert resp.status_code == 401
    assert "Login failed" in resp.text
    assert session.current_client() is None


def test_logout_clears_session_and_redirects(monkeypatch):
    client_factory(monkeypatch, session)
    with TestClient(app) as client:
        client.post("/login", data={"login": "user@example.com", "password": "hunter2"})
        resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert session.current_client() is None


def test_library_requires_login():
    with TestClient(app) as client:
        resp = client.get("/library")
    assert resp.status_code == 401
    assert resp.json()["ok"] is False


def test_library_returns_expected_shape(monkeypatch):
    library = [
        {
            "id": 1,
            "title": "Book One",
            "art_type": 0,
            "persons": [
                {"full_name": "Author A", "role": "author"},
                {"full_name": "Translator T", "role": "translator"},
            ],
            "cover_url": "/pub/c/cover/1.jpg",
        },
        {"id": 2, "title": "Audiobook Two", "art_type": 1, "persons": [], "cover_url": None},
    ]
    client_factory(monkeypatch, session, library=library)

    with TestClient(app) as client:
        client.post("/login", data={"login": "user@example.com", "password": "hunter2"})
        resp = client.get("/library")

    assert resp.status_code == 200
    books = resp.json()["books"]
    assert books[0] == {
        "id": 1,
        "title": "Book One",
        "authors": "Author A",  # translator excluded, only role == author
        "is_audio": False,
        "cover_url": "https://static.litres.ru/pub/c/cover/1.jpg",
    }
    assert books[1] == {
        "id": 2,
        "title": "Audiobook Two",
        "authors": "",
        "is_audio": True,
        "cover_url": None,
    }


def test_library_second_request_is_served_from_cache_not_refetched(monkeypatch):
    fake = client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book One"}])
    calls = []
    original = fake.iter_library

    def counting_iter_library(limit=100):
        calls.append(1)
        return original(limit)

    fake.iter_library = counting_iter_library

    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        first = client.get("/library")
        second = client.get("/library")

    assert first.json() == second.json()
    assert len(calls) == 1  # second request hit the cache, not litres.ru again


def test_library_serves_stale_cache_while_an_activity_is_busy(monkeypatch):
    # When the fresh cache has expired AND an activity is in progress (the one
    # worker thread is busy, e.g. a large download), the route must serve the
    # slightly-stale cached list rather than block on a live re-fetch -- so the
    # library never appears to vanish mid-download.
    from bookvault_core import cache

    fake = client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book One"}])
    calls = []
    original = fake.iter_library

    def counting_iter_library(limit=100):
        calls.append(1)
        return original(limit)

    fake.iter_library = counting_iter_library

    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        warm = client.get("/library")  # warms the cache (one live fetch)
        monkeypatch.setattr(cache, "LIBRARY_TTL", 0)  # fresh cache now considered expired
        monkeypatch.setattr(activity, "_state", {**activity._state, "state": activity.PREPARING})
        resp = client.get("/library")

    assert resp.status_code == 200
    assert resp.json() == warm.json()  # same (now-stale) list served, not blanked
    assert len(calls) == 1  # served from stale cache -- no second (blocking) fetch


def test_library_refresh_param_bypasses_the_cache(monkeypatch):
    fake = client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book One"}])
    calls = []
    original = fake.iter_library

    def counting_iter_library(limit=100):
        calls.append(1)
        return original(limit)

    fake.iter_library = counting_iter_library

    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        client.get("/library")
        client.get("/library?refresh=true")

    assert len(calls) == 2


def test_book_size_second_request_is_served_from_cache_not_refetched(monkeypatch):
    fake = client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 2_400_000}]},
    )
    calls = []
    original = fake.get_files

    def counting_get_files(art_id, should_cancel=None):
        calls.append(art_id)
        return original(art_id)

    fake.get_files = counting_get_files

    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        first = client.get("/library/1/size")
        second = client.get("/library/1/size")

    assert first.json() == {"ok": True, "size_mb": 2.4, "cached": False}
    assert second.json() == {"ok": True, "size_mb": 2.4, "cached": True}
    assert calls == [1]  # second request hit the cache, not litres.ru again


def test_library_returns_clean_error_instead_of_crashing(monkeypatch):
    # Regression test: a transient network failure, an anti-bot block, or a
    # client reference left stale by a login/logout race (see session.py)
    # used to bubble up as an unhandled 500 with a raw traceback.
    fake = client_factory(monkeypatch, session, library=[])

    def broken_iter_library(limit=100):
        raise RuntimeError("socket hang up")
        yield  # pragma: no cover -- makes this a generator

    fake.iter_library = broken_iter_library

    with TestClient(app) as client:
        client.post("/login", data={"login": "user@example.com", "password": "hunter2"})
        resp = client.get("/library")

    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def test_book_size_requires_login():
    with TestClient(app) as client:
        resp = client.get("/library/1/size")
    assert resp.status_code == 401


def test_book_size_returns_clean_error_instead_of_crashing(monkeypatch):
    fake = client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book One"}])

    def broken_get_files(art_id, should_cancel=None):
        raise RuntimeError("Event loop is closed! Is Playwright already stopped?")

    fake.get_files = broken_get_files

    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.get("/library/1/size")

    assert resp.status_code == 503
    assert resp.json()["ok"] is False


def test_book_size_returns_mb_for_available_file(monkeypatch):
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 2_400_000}]},
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.get("/library/1/size")
    assert resp.json() == {"ok": True, "size_mb": 2.4, "cached": False}


def test_book_size_is_none_when_no_downloadable_file(monkeypatch):
    client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book One"}], files_by_id={1: []})
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.get("/library/1/size")
    assert resp.json() == {"ok": True, "size_mb": None, "cached": False}


def test_activity_status_default_shape_when_idle():
    with TestClient(app) as client:
        resp = client.get("/activity")
    body = resp.json()
    assert body["state"] == "idle"
    assert body["result"] is None
    assert body["log"] == []
    assert body["sizes"] == {}


def test_activity_prepare_requires_login():
    with TestClient(app) as client:
        resp = client.post("/activity/prepare", json={})
    assert resp.status_code == 401


def test_activity_refresh_requires_login():
    with TestClient(app) as client:
        resp = client.post("/activity/refresh", json={})
    assert resp.status_code == 401


def test_activity_check_requires_login():
    with TestClient(app) as client:
        resp = client.post("/activity/check", json={})
    assert resp.status_code == 401


def test_activity_prepare_rejects_explicitly_empty_selection(monkeypatch):
    client_factory(monkeypatch, session, library=[{"id": 1, "title": "Book"}])
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.post("/activity/prepare", json={"art_ids": []})
    assert resp.status_code == 400
    assert "No books selected" in resp.json()["error"]


def test_activity_prepare_with_no_art_ids_downloads_everything(monkeypatch):
    fake = client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 10}]},
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.post("/activity/prepare", json={})
        assert resp.json() == {"ok": True, "started": True}
    _wait_until_idle()
    assert fake.download_calls == [1]


def test_activity_prepare_returns_started_false_when_already_running(monkeypatch):
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 10}]},
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        first = client.post("/activity/prepare", json={})
        second = client.post("/activity/prepare", json={})
    assert first.json()["started"] is True
    assert second.json()["started"] is False
    _wait_until_idle()


def test_activity_refresh_reloads_library_and_reports_via_status(monkeypatch):
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book", "art_type": 0, "persons": [], "cover_url": None}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 2_400_000}]},
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        resp = client.post("/activity/refresh", json={})
        assert resp.json() == {"ok": True, "started": True}
        final = _wait_until_idle()
        # Sizes come back keyed by id (as JSON string keys over the wire).
        status = client.get("/activity").json()
    assert final["result"] == "done"
    assert status["sizes"] == {"1": 2.4}


def test_activity_check_sweeps_sizes(monkeypatch):
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 1_000_000}]},
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        client.get("/library")  # warm the library cache the sweep reads from
        resp = client.post("/activity/check", json={"selected": [1]})
        assert resp.json() == {"ok": True, "started": True}
        _wait_until_idle()
        status = client.get("/activity").json()
    assert status["result"] == "done"
    assert status["sizes"] == {"1": 1.0}


def test_activity_cancel_returns_false_when_nothing_running():
    with TestClient(app) as client:
        resp = client.post("/activity/cancel")
    assert resp.json() == {"ok": True, "cancelled": False}


def test_download_file_redirects_when_nothing_prepared_yet():
    with TestClient(app) as client:
        resp = client.get("/download/file", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_download_file_serves_the_completed_zip(monkeypatch):
    client_factory(
        monkeypatch,
        session,
        library=[{"id": 1, "title": "Book One"}],
        files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 10}]},
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        client.post("/activity/prepare", json={})
        _wait_until_idle()
        resp = client.get("/download/file")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.content  # non-empty zip bytes


def test_results_and_download_survive_the_reload_size_check_over_http(monkeypatch):
    """End-to-end over the real routes: after a build, the automatic on-load
    size-check must not wipe the results view or the download link -- so a user
    who reloads to inspect failures still finds them (and can still download)."""
    client_factory(
        monkeypatch,
        session,
        library=[
            {"id": 1, "title": "Good", "art_type": 0, "persons": [], "cover_url": None},
            {"id": 2, "title": "Bad", "art_type": 0, "persons": [], "cover_url": None},
        ],
        files_by_id={
            1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 8}],
            2: [{"id": 200, "extension": "epub", "is_additional": False, "size": 8}],
        },
    )
    with TestClient(app) as client:
        client.post("/login", data={"login": "u@example.com", "password": "pw"})
        # book 2 fails -> one success, one error in the results
        session.current_client().fail_downloads = {2}
        client.post("/activity/prepare", json={"art_ids": [1, 2]})
        _wait_until_idle()

        # the size-check that fires on the next page load
        client.post("/activity/check", json={"selected": [], "live": False})
        _wait_until_idle()

        snap = client.get("/activity").json()

    # live log was reset by the check, but the durable results + zip survive
    assert snap["log"] == []
    statuses = sorted(e["status"] for e in snap["results"])
    assert statuses == ["done", "error"]
    assert [e["title"] for e in snap["results"] if e["status"] == "error"] == ["Bad"]
    assert snap["zip_path"]  # download link still available after reload


def test_activity_route_embeds_prefs_end_to_end(monkeypatch):
    """A browser only polls /activity; it must carry the shared selection +
    formats so any browser hydrates the same view."""
    client_factory(monkeypatch, session, library=[])
    with TestClient(app) as client:
        client.post("/prefs", json={"selected": [3, 1], "ebook_format": "epub"})
        snap = client.get("/activity").json()
    assert snap["prefs"] == {"selected": [3, 1], "ebook_format": "epub", "audiobook_format": None}
