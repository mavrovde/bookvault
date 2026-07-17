"""Tests for bookvault_core/session.py: restore/login/logout precedence and
fallbacks, and the dedicated-thread execution model (run/submit/run_async)
that everything else relies on for Playwright thread-affinity."""
from __future__ import annotations

import threading

import pytest

from bookvault_core import cache, credentials, session
from bookvault_core.client import LitresAuthError
from tests.fakes import FakeLitresClient, client_factory

# --------------------------------------------------------------------------
# restore_session
# --------------------------------------------------------------------------


def test_restore_session_noop_when_nothing_saved(monkeypatch):
    client_factory(monkeypatch, session)
    session.restore_session()
    assert session.current_client() is None
    assert session.current_login() is None


def test_restore_session_reuses_valid_saved_session_file(monkeypatch):
    session.SESSION_STATE_PATH.write_text("{}")
    credentials.save("user@example.com", "hunter2")  # so the display login can be recovered
    fake = client_factory(monkeypatch, session, library=[])
    fake._is_logged_in = True

    session.restore_session()

    assert session.current_client() is fake
    assert session.current_login() == "user@example.com"
    assert fake.login_calls == []  # reused the saved session, no fresh login


def test_restore_session_from_cookies_falls_back_to_env_login_without_keyring(monkeypatch):
    """Regression test: a valid cookie session has no login name of its own
    (cookies don't carry one -- see login()/credentials.py) -- if there's no
    keyring entry either, the display login must still come from
    LITRES_LOGIN rather than silently ending up as None."""
    session.SESSION_STATE_PATH.write_text("{}")
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    fake = client_factory(monkeypatch, session, library=[])
    fake._is_logged_in = True

    session.restore_session()

    assert session.current_client() is fake
    assert session.current_login() == "envuser@example.com"
    assert fake.login_calls == []  # reused the saved session, no fresh login


def test_restore_session_falls_back_to_keyring_when_saved_session_stale(monkeypatch):
    session.SESSION_STATE_PATH.write_text("{}")
    credentials.save("user@example.com", "hunter2")
    fake = client_factory(monkeypatch, session)
    fake._is_logged_in = False  # the saved cookies no longer work

    session.restore_session()

    assert session.current_client() is fake
    assert fake.login_calls == [("user@example.com", "hunter2")]


def test_restore_session_uses_env_vars_when_no_keyring_creds(monkeypatch):
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    monkeypatch.setenv("LITRES_PASSWORD", "envpass")
    fake = client_factory(monkeypatch, session)

    session.restore_session()

    assert fake.login_calls == [("envuser@example.com", "envpass")]
    assert session.current_login() == "envuser@example.com"


def test_restore_session_prefers_keyring_over_env(monkeypatch):
    credentials.save("keyringuser@example.com", "keyringpass")
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    monkeypatch.setenv("LITRES_PASSWORD", "envpass")
    fake = client_factory(monkeypatch, session)

    session.restore_session()

    assert fake.login_calls == [("keyringuser@example.com", "keyringpass")]


def test_restore_session_handles_bad_saved_credentials_gracefully(monkeypatch):
    credentials.save("user@example.com", "wrongpass")
    fake = client_factory(monkeypatch, session)
    fake.fail_login = True

    session.restore_session()  # must not raise

    assert session.current_client() is None
    assert fake.closed is True


def test_restore_session_is_a_noop_if_already_restored(monkeypatch):
    credentials.save("user@example.com", "hunter2")
    fake = client_factory(monkeypatch, session)

    session.restore_session()
    session.restore_session()

    assert fake.login_calls == [("user@example.com", "hunter2")]  # not called twice


# --------------------------------------------------------------------------
# restore_session(allow_env_login=False) -- the web-app flow. The web UI never
# bootstraps a login from .env credentials (that's MCP-only); it restores a
# saved session or re-logs-in from the keychain, and otherwise stays logged
# out so its login form is shown.
# --------------------------------------------------------------------------


def test_web_restore_ignores_env_credentials_and_stays_logged_out(monkeypatch):
    """The whole point: even with LITRES_LOGIN/PASSWORD set, the web flow
    must not auto-login from them -- no saved session, no keychain => the
    login form is shown, not a silent env-credential login."""
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    monkeypatch.setenv("LITRES_PASSWORD", "envpass")
    fake = client_factory(monkeypatch, session)

    session.restore_session(allow_env_login=False)

    assert session.current_client() is None
    assert fake.login_calls == []  # never touched the env credentials


def test_web_restore_still_reuses_a_saved_session(monkeypatch):
    session.SESSION_STATE_PATH.write_text("{}")
    credentials.save("user@example.com", "hunter2")  # display name source
    fake = client_factory(monkeypatch, session, library=[])
    fake._is_logged_in = True

    session.restore_session(allow_env_login=False)

    assert session.current_client() is fake
    assert session.current_login() == "user@example.com"
    assert fake.login_calls == []  # reused the saved session, no fresh login


def test_web_restore_still_relogins_from_keyring(monkeypatch):
    """Keeping the keychain convenience: when the cookie session has lapsed
    but the OS keychain still holds credentials, the web app silently
    re-logs-in -- it just never reaches for .env to do so."""
    session.SESSION_STATE_PATH.write_text("{}")
    credentials.save("user@example.com", "hunter2")
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    monkeypatch.setenv("LITRES_PASSWORD", "envpass")
    fake = client_factory(monkeypatch, session)
    fake._is_logged_in = False  # saved cookies no longer work

    session.restore_session(allow_env_login=False)

    assert fake.login_calls == [("user@example.com", "hunter2")]  # keychain, not env


def test_restore_session_recovers_login_from_account_when_no_name_known(monkeypatch):
    """A cookie-only restore with no keychain entry and no env login (the Docker
    case) recovers the real account identity from /users/me, so the UI shows the
    email instead of a generic "Signed in" -- not the env name, not None."""
    session.SESSION_STATE_PATH.write_text("{}")
    fake = client_factory(monkeypatch, session, library=[])
    fake._is_logged_in = True
    fake.account_identity = "account@example.com"  # what /users/me yields

    session.restore_session(allow_env_login=False)

    assert session.current_client() is fake
    assert session.current_login() == "account@example.com"
    assert fake.login_calls == []  # reused the saved session, no fresh login


def test_restore_session_prefers_keychain_name_over_account_lookup(monkeypatch):
    """When the keychain already knows the login, that wins -- no need to fall
    back to the /users/me lookup."""
    session.SESSION_STATE_PATH.write_text("{}")
    credentials.save("typed@example.com", "hunter2")
    fake = client_factory(monkeypatch, session, library=[])
    fake._is_logged_in = True
    fake.account_identity = "other@web.de"  # would be used only as a fallback

    session.restore_session()

    assert session.current_login() == "typed@example.com"


def test_web_restore_from_cookies_does_not_borrow_env_login_name(monkeypatch):
    """A valid cookie session with no keychain entry restores fine, but its
    display login stays None on the web flow rather than being filled in from
    LITRES_LOGIN (which the web app must ignore entirely)."""
    session.SESSION_STATE_PATH.write_text("{}")
    monkeypatch.setenv("LITRES_LOGIN", "envuser@example.com")
    fake = client_factory(monkeypatch, session, library=[])
    fake._is_logged_in = True

    session.restore_session(allow_env_login=False)

    assert session.current_client() is fake
    assert session.current_login() is None  # not borrowed from env
    assert fake.login_calls == []


# --------------------------------------------------------------------------
# login / logout / shutdown
# --------------------------------------------------------------------------


def test_login_persists_session_state_and_credentials(monkeypatch):
    fake = client_factory(monkeypatch, session)

    session.login("user@example.com", "hunter2")

    assert session.current_client() is fake
    assert session.current_login() == "user@example.com"
    assert fake.saved_state_path == session.SESSION_STATE_PATH
    assert credentials.load_last() == ("user@example.com", "hunter2")


def test_login_closes_the_previous_client_before_replacing_it(monkeypatch):
    old_fake = FakeLitresClient()
    session._state["client"], session._state["login"] = old_fake, "old@example.com"
    new_fake = client_factory(monkeypatch, session)

    session.login("new@example.com", "pw")

    assert old_fake.closed is True
    assert session.current_client() is new_fake


def test_login_failure_raises_and_closes_the_failed_client(monkeypatch):
    fake = client_factory(monkeypatch, session)
    fake.fail_login = True

    with pytest.raises(LitresAuthError):
        session.login("user@example.com", "wrongpass")

    assert fake.closed is True
    assert session.current_client() is None


def test_logout_clears_client_credentials_and_session_file(monkeypatch):
    client_factory(monkeypatch, session)
    session.login("user@example.com", "hunter2")
    assert session.SESSION_STATE_PATH.exists()

    session.logout()

    assert session.current_client() is None
    assert session.current_login() is None
    assert credentials.load_last() is None
    assert not session.SESSION_STATE_PATH.exists()


def test_logout_when_nothing_logged_in_does_not_raise():
    session.logout()  # must not raise
    assert session.current_client() is None


def test_login_clears_the_cache_so_a_different_account_cant_leak_in(monkeypatch):
    cache.set_library([{"id": 1, "title": "Stale book from a previous account"}])
    client_factory(monkeypatch, session)

    session.login("user@example.com", "hunter2")

    assert cache.get_library() is None


def test_logout_clears_the_cache(monkeypatch):
    client_factory(monkeypatch, session)
    session.login("user@example.com", "hunter2")
    cache.set_library([{"id": 1, "title": "Book One"}])

    session.logout()

    assert cache.get_library() is None


def test_shutdown_closes_client_but_keeps_saved_credentials(monkeypatch):
    fake = client_factory(monkeypatch, session)
    session.login("user@example.com", "hunter2")

    session.shutdown()

    assert fake.closed is True
    assert session.current_client() is None
    assert credentials.load_last() == ("user@example.com", "hunter2")  # unlike logout()


def test_shutdown_when_nothing_logged_in_does_not_raise():
    session.shutdown()  # must not raise


# --------------------------------------------------------------------------
# run / submit / run_async -- the dedicated-thread execution model
# --------------------------------------------------------------------------


def test_run_executes_on_the_dedicated_playwright_thread():
    name = session.run(lambda: threading.current_thread().name)
    assert name.startswith("litres-playwright")
    assert name != threading.current_thread().name


def test_run_propagates_exceptions():
    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        session.run(boom)


def test_submit_returns_a_future_with_the_right_result():
    future = session.submit(lambda: 1 + 1)
    assert future.result(timeout=2) == 2


@pytest.mark.asyncio
async def test_run_async_returns_result():
    result = await session.run_async(lambda x: x + 1, 41)
    assert result == 42


@pytest.mark.asyncio
async def test_run_async_propagates_exceptions():
    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await session.run_async(boom)


# --------------------------------------------------------------------------
# Resource safety / boot resilience: unexpected (non-auth) failures must
# never leak a Chromium client or crash an unattended restore.
# --------------------------------------------------------------------------


def test_login_with_unexpected_error_closes_client_and_raises_auth_error(monkeypatch):
    """A NON-auth failure inside client.login (e.g. a Playwright timeout when
    litres.ru shows a captcha) must still close the browser it was driving,
    and surface as a LitresAuthError so the web route renders a clean banner
    instead of a raw 500."""
    fake = client_factory(monkeypatch, session)
    fake.login_exception = RuntimeError("Page.wait_for_selector: Timeout 15000ms exceeded")

    with pytest.raises(LitresAuthError, match="did not complete"):
        session.login("user@example.com", "hunter2")

    assert fake.closed is True
    assert session.current_client() is None


def test_restore_session_survives_a_crashing_session_validation(monkeypatch):
    """Startup restore is best-effort: litres.ru being unreachable while
    validating saved cookies must leave the app logged out -- NOT crash the
    lifespan (which would take the whole web/desktop boot down with it)."""
    session.SESSION_STATE_PATH.write_text("{}")  # a saved session exists
    fake = client_factory(monkeypatch, session)
    fake.is_logged_in_exception = ConnectionError("net::ERR_INTERNET_DISCONNECTED")

    session.restore_session()  # must not raise

    assert session.current_client() is None
    assert fake.closed is True
    # The cookies were never *validated* as bad -- keep the file so the next
    # start (with the network back) can still restore without a fresh login.
    assert session.SESSION_STATE_PATH.exists()


def test_restore_session_survives_an_unexpected_auto_login_error(monkeypatch):
    """Same contract for the keychain auto-login fallback: a non-auth crash
    mid-login stays logged out and closes the client instead of raising."""
    credentials.save("user@example.com", "hunter2")
    fake = client_factory(monkeypatch, session)
    fake.login_exception = RuntimeError("browser closed unexpectedly")

    session.restore_session()  # must not raise

    assert session.current_client() is None
    assert fake.closed is True
