"""Live smoke tests -- opt-in, run against a really-running server.

Unlike test_e2e_smoke.py (offline, fake backend), these hit an actual
running instance over HTTP -- e.g. the Docker web app on 127.0.0.1:8420 with
a real logged-in litres.ru session. They talk to real litres.ru, need a valid
session, and can't run in CI, so they're marked `live` and DESELECTED by
default (see `addopts = -m "not live"` in pyproject.toml).

Run them explicitly against the running app:

    pytest -m live                                  # default base URL :8420
    BOOKVAULT_BASE_URL=http://127.0.0.1:8420 pytest -m live

A test skips (rather than fails) when the server is unreachable or logged
out, so `-m live` is safe to run even when nothing is up.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("BOOKVAULT_BASE_URL", "http://127.0.0.1:8420").rstrip("/")


def _get(path: str):
    """GET BASE_URL+path -> (status, body_text). Skips the test if the server
    isn't reachable at all (nothing running to smoke-test)."""
    url = f"{BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # server up, non-2xx -- a real result
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        pytest.skip(f"no running server at {BASE_URL} ({exc}) -- start it, or skip live tests")


def test_live_home_serves():
    status, body = _get("/")
    assert status == 200
    # either the login form (logged out) or the session chip (logged in)
    assert 'name="login"' in body or 'class="session-chip"' in body


def test_live_activity_endpoint_reports_a_state():
    status, body = _get("/activity")
    assert status == 200
    snap = json.loads(body)
    assert "state" in snap


def test_live_library_lists_owned_books():
    status, body = _get("/library")
    if status == 401:
        pytest.skip("server is running but logged out -- log in, then re-run -m live")
    assert status == 200
    payload = json.loads(body)
    books = payload.get("books", [])
    # A logged-in account should own at least one title; assert the shape too.
    assert isinstance(books, list) and len(books) >= 1
    first = books[0]
    assert {"id", "title", "is_audio"} <= set(first)
