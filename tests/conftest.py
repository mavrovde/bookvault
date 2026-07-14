"""Shared fixtures. The two autouse ones are safety nets that apply to
every test in the suite: never touch the real OS keychain, never leak the
real .env credentials or a prior test's session/job/cache state into the
next test."""
from __future__ import annotations

import pytest

from litres_core import cache, credentials, session
from litres_web import activity
from tests.fakes import FakeKeyring


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """No test should ever touch the real macOS Keychain."""
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "keyring", fake)
    return fake


@pytest.fixture(autouse=True)
def isolated_module_state(tmp_path, monkeypatch):
    """Reset the module-level singletons in session.py/activity.py/cache.py
    before and after every test, and keep the real .env's credentials (if
    any) out of the test environment entirely."""
    monkeypatch.delenv("LITRES_LOGIN", raising=False)
    monkeypatch.delenv("LITRES_PASSWORD", raising=False)
    monkeypatch.setattr(session, "SESSION_STATE_PATH", tmp_path / ".litres_session.json")
    monkeypatch.setattr(cache, "CACHE_PATH", tmp_path / ".litres_cache.json")
    # No real pacing sleep between size fetches in tests -- the sweep's
    # behaviour is what's under test, not litres.ru-friendliness timing.
    monkeypatch.setattr(activity, "PACE_SECONDS", 0)

    def _reset():
        session._state["client"] = None
        session._state["login"] = None
        activity._cancel_event.clear()
        activity._state.update(
            state=activity.IDLE,
            result=None,
            message="",
            current_title=None,
            done=0,
            total=None,
            log=[],
            error=None,
            sizes={},
            zip_path=None,
        )
        cache._state = None

    _reset()
    yield
    _reset()
