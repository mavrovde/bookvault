"""Tests for the server-side shared UI state (bookvault_web/prefs.py) and its
HTTP surface -- the selection + format prefs that make every browser show the
same view."""
from __future__ import annotations

from fastapi.testclient import TestClient

from bookvault_core import session
from bookvault_web import prefs
from bookvault_web.app import app
from tests.fakes import client_factory


# -- the store itself -------------------------------------------------------

def test_snapshot_defaults_when_nothing_set():
    assert prefs.snapshot() == {"selected": [], "ebook_format": None, "audiobook_format": None}


def test_update_is_partial_and_does_not_clobber_other_fields():
    prefs.update(ebook_format="epub")
    prefs.update(selected=[3, 1, 2])
    snap = prefs.snapshot()
    assert snap["ebook_format"] == "epub"      # survived the second update
    assert snap["selected"] == [3, 1, 2]
    assert snap["audiobook_format"] is None


def test_update_normalises_selection_to_unique_ints_order_stable():
    prefs.update(selected=["5", 5, 3, "3", 7])
    assert prefs.snapshot()["selected"] == [5, 3, 7]


def test_update_filters_out_non_numeric_selection_values():
    prefs.update(selected=[1, "two", None, 3, 3.0])  # "two"/None dropped, 3.0 dedups to 3
    assert prefs.snapshot()["selected"] == [1, 3]


def test_empty_selection_clears_it_but_none_leaves_it_untouched():
    prefs.update(selected=[1, 2, 3])
    prefs.update(ebook_format="epub")          # selected=None -> untouched
    assert prefs.snapshot()["selected"] == [1, 2, 3]
    prefs.update(selected=[])                   # explicit empty -> cleared
    assert prefs.snapshot()["selected"] == []


def test_both_formats_can_be_set_at_once():
    prefs.update(ebook_format="fb2", audiobook_format="mp3")
    snap = prefs.snapshot()
    assert (snap["ebook_format"], snap["audiobook_format"]) == ("fb2", "mp3")


def test_corrupt_state_file_starts_fresh_instead_of_crashing():
    prefs.STATE_PATH.write_text("{ this is not valid json")
    prefs._state = None  # force a reload from disk
    assert prefs.snapshot() == {"selected": [], "ebook_format": None, "audiobook_format": None}


def test_unknown_keys_in_state_file_are_ignored():
    prefs.STATE_PATH.write_text('{"selected": [5], "junk": "x", "ebook_format": "fb2"}')
    prefs._state = None
    assert prefs.snapshot() == {"selected": [5], "ebook_format": "fb2", "audiobook_format": None}


def test_reset_clears_state_and_removes_the_file():
    prefs.update(selected=[1], ebook_format="epub")
    assert prefs.STATE_PATH.exists()
    prefs.reset()
    assert prefs.snapshot() == {"selected": [], "ebook_format": None, "audiobook_format": None}
    assert not prefs.STATE_PATH.exists()


def test_state_persists_to_disk_across_a_reload(monkeypatch, tmp_path):
    # conftest points STATE_PATH at a tmp file; write, drop the in-memory
    # cache, and confirm it reloads from disk (survives a process restart).
    prefs.update(selected=[9], audiobook_format="mp3")
    prefs._state = None  # simulate a fresh process
    snap = prefs.snapshot()
    assert snap["selected"] == [9]
    assert snap["audiobook_format"] == "mp3"


# -- the HTTP surface -------------------------------------------------------

def test_get_prefs_returns_current_state():
    prefs.update(selected=[1, 2], ebook_format="fb2")
    with TestClient(app) as client:
        resp = client.get("/prefs")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "selected": [1, 2], "ebook_format": "fb2", "audiobook_format": None}


def test_post_prefs_updates_and_persists():
    with TestClient(app) as client:
        r1 = client.post("/prefs", json={"selected": [4, 5]})
        assert r1.json()["selected"] == [4, 5]
        r2 = client.post("/prefs", json={"ebook_format": "epub"})
        # partial update keeps the selection
        assert r2.json() == {"ok": True, "selected": [4, 5], "ebook_format": "epub", "audiobook_format": None}


def test_activity_snapshot_embeds_prefs_for_cross_browser_sync(monkeypatch):
    """The /activity poll carries the shared prefs, so any browser hydrates the
    same ticked books + formats from the response it already fetches."""
    client_factory(monkeypatch, session, library=[])
    prefs.update(selected=[7], ebook_format="pdf")
    with TestClient(app) as client:
        resp = client.get("/activity")
    body = resp.json()
    assert "state" in body  # still the activity snapshot
    assert body["prefs"] == {"selected": [7], "ebook_format": "pdf", "audiobook_format": None}


def test_selection_set_in_one_client_is_visible_to_another(monkeypatch):
    """The whole point: browser A's selection is on the server, so browser B
    (a separate client) sees it."""
    with TestClient(app) as browser_a:
        browser_a.post("/prefs", json={"selected": [11, 22]})
    with TestClient(app) as browser_b:
        resp = browser_b.get("/prefs")
    assert resp.json()["selected"] == [11, 22]


def test_prefs_endpoints_work_without_login():
    """Prefs are UI state, independent of the litres session -- reading/writing
    them must not require being logged in (the page itself does)."""
    with TestClient(app) as client:
        assert client.get("/prefs").status_code == 200
        assert client.post("/prefs", json={"selected": [1]}).status_code == 200


def test_post_prefs_with_empty_body_is_a_noop():
    prefs.update(selected=[1], ebook_format="epub")
    with TestClient(app) as client:
        resp = client.post("/prefs", json={})
    assert resp.json() == {"ok": True, "selected": [1], "ebook_format": "epub", "audiobook_format": None}


def test_save_is_atomic_and_leaves_no_tmp_file_behind():
    import json

    prefs.update(selected=[1, 2])
    assert not prefs.STATE_PATH.with_name(prefs.STATE_PATH.name + ".tmp").exists()
    assert json.loads(prefs.STATE_PATH.read_text())["selected"] == [1, 2]
