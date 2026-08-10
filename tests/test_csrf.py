"""Cross-origin write protection for the local app (issue #41).

The app binds 127.0.0.1 with no auth and no CSRF token by design, which means
any page the user has open can POST to it. These tests pin the rule that
closes that: state-changing verbs must come from the app's own UI (or from a
non-browser caller), never from another website.
"""
from __future__ import annotations

import pytest
from bookvault_core import session
from bookvault_web import folder_dialog, prefs
from bookvault_web.app import app
from fastapi.testclient import TestClient

from tests.fakes import client_factory

# Every state-changing route on the app. Listed explicitly rather than
# discovered, so a newly added POST that forgets the check shows up as a
# failure here (see test_every_state_changing_route_is_covered).
WRITE_ROUTES = [
    ("/login", {"data": {"login": "u@example.com", "password": "pw"}}),
    ("/logout", {}),
    ("/prefs", {"json": {"ebook_format": "epub"}}),
    ("/prefs/browse", {}),
    ("/activity/prepare", {"json": {}}),
    ("/activity/refresh", {"json": {}}),
    ("/activity/check", {"json": {}}),
    ("/activity/sync", {"json": {}}),
    ("/activity/cancel", {}),
]

FOREIGN = "https://evil.example.com"


@pytest.mark.parametrize("path,kwargs", WRITE_ROUTES, ids=[r[0] for r in WRITE_ROUTES])
def test_a_foreign_origin_is_refused_on_every_write_route(path, kwargs):
    with TestClient(app) as client:
        resp = client.post(path, headers={"origin": FOREIGN}, follow_redirects=False, **kwargs)
    assert resp.status_code == 403, f"{path} accepted a cross-origin write"
    assert resp.json()["ok"] is False


@pytest.mark.parametrize("path,kwargs", WRITE_ROUTES, ids=[r[0] for r in WRITE_ROUTES])
def test_a_cross_site_fetch_is_refused_on_every_write_route(path, kwargs):
    """Sec-Fetch-Site is set by the browser and can't be forged by page JS, so
    it's checked first."""
    with TestClient(app) as client:
        resp = client.post(
            path, headers={"sec-fetch-site": "cross-site"}, follow_redirects=False, **kwargs
        )
    assert resp.status_code == 403, f"{path} accepted a cross-site write"


def test_same_site_is_also_refused():
    """`same-site` is a *different origin* on the same registrable domain --
    still not our UI."""
    with TestClient(app) as client:
        resp = client.post("/prefs", json={}, headers={"sec-fetch-site": "same-site"})
    assert resp.status_code == 403


# -- what must keep working -------------------------------------------------

def test_the_apps_own_ui_is_allowed():
    with TestClient(app) as client:
        resp = client.post("/prefs", json={"ebook_format": "epub"}, headers={"sec-fetch-site": "same-origin"})
    assert resp.status_code == 200
    assert prefs.snapshot()["ebook_format"] == "epub"


def test_a_typed_url_or_bookmark_is_allowed():
    """`none` means the user themselves initiated it, not a page."""
    with TestClient(app) as client:
        resp = client.post("/prefs", json={"ebook_format": "fb2"}, headers={"sec-fetch-site": "none"})
    assert resp.status_code == 200


def test_a_matching_origin_without_sec_fetch_site_is_allowed():
    """The Linux desktop build runs on WebKitGTK, which may not send
    Sec-Fetch-Site -- the Origin fallback is what keeps that window working."""
    with TestClient(app) as client:
        resp = client.post(
            "/prefs", json={"ebook_format": "epub"}, headers={"origin": "http://testserver"}
        )
    assert resp.status_code == 200


def test_a_non_browser_caller_is_allowed():
    """curl, a local script, the live smoke tests: no Origin, no Sec-Fetch-Site.
    Code already running as the user could talk to the app regardless -- the
    threat model is a page the user visited."""
    with TestClient(app) as client:
        resp = client.post("/prefs", json={"ebook_format": "epub"})
    assert resp.status_code == 200


def test_reads_are_untouched_even_from_a_foreign_page():
    """Only state-changing verbs are checked; a GET mutates nothing."""
    with TestClient(app) as client:
        for path in ("/", "/activity", "/prefs"):
            assert client.get(path, headers={"origin": FOREIGN}).status_code == 200


def test_the_login_form_still_works_from_the_apps_own_page(monkeypatch):
    """The one write that's a real HTML form post rather than a fetch."""
    client_factory(monkeypatch, session)
    with TestClient(app) as client:
        resp = client.post(
            "/login",
            data={"login": "user@example.com", "password": "pw"},
            headers={"sec-fetch-site": "same-origin"},
        )
    assert resp.status_code == 200
    assert session.current_login() == "user@example.com"


# -- the consequences the issue actually cares about ------------------------

def test_a_foreign_page_cannot_start_a_build(monkeypatch):
    """"A page you visited started a several-hour download from your account.\""""
    started = []
    monkeypatch.setattr(prefs, "DEFAULT_DOWNLOAD_DIR", None)
    monkeypatch.setattr("bookvault_web.app.activity.prepare", lambda *a, **k: started.append(1))
    with TestClient(app) as client:
        resp = client.post("/activity/prepare", json={}, headers={"origin": FOREIGN})
    assert resp.status_code == 403
    assert started == []


def test_a_foreign_page_cannot_pop_a_native_folder_dialog(monkeypatch):
    """The picker added in this release is exactly the kind of endpoint the
    issue is about -- it opens a window on the user's desktop."""
    opened = []
    monkeypatch.setattr(folder_dialog, "is_available", lambda: True)
    monkeypatch.setattr(folder_dialog, "choose_folder", lambda initial=None: opened.append(1))
    with TestClient(app) as client:
        resp = client.post("/prefs/browse", headers={"sec-fetch-site": "cross-site"})
    assert resp.status_code == 403
    assert opened == []


def test_a_foreign_page_cannot_change_the_save_folder(tmp_path):
    with TestClient(app) as client:
        resp = client.post(
            "/prefs", json={"download_dir": str(tmp_path)}, headers={"origin": FOREIGN}
        )
    assert resp.status_code == 403
    assert prefs.snapshot()["download_dir"] is None


def test_a_foreign_page_cannot_log_the_user_out(monkeypatch):
    client_factory(monkeypatch, session)
    with TestClient(app) as client:
        client.post("/login", data={"login": "user@example.com", "password": "pw"})
        resp = client.post("/logout", headers={"origin": FOREIGN}, follow_redirects=False)
    assert resp.status_code == 403
    assert session.current_login() == "user@example.com"  # still signed in


def test_every_state_changing_route_is_covered():
    """A new POST added without a thought for this check should fail here
    rather than quietly become reachable from any page."""
    declared = {path for path, _ in WRITE_ROUTES}
    actual = {
        route.path
        for route in app.routes
        if getattr(route, "methods", None) and route.methods & {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert actual == declared, f"untested state-changing routes: {actual - declared}"
