"""Shared login/session-restore logic for both the web UI and the MCP server.

Both entry points need the same "restore a saved session, or bootstrap
from .env, or fail" dance -- kept in one place so they can't drift.

Playwright's sync API is tied to whichever single thread created it -- every
call touching a `LitresClient` (this module's own login/restore code, but
also `web.py`'s /library route and `activity.py`'s background activities) MUST
run on that same thread or it fails with "Cannot switch to a different
thread". `run`/`run_async` below are the one gateway to that dedicated
thread; nothing else should call a LitresClient method directly.
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import cache, credentials
from .client import LitresAuthError, LitresClient

logger = logging.getLogger(__name__)

# Defaults to `.litres_session.json` in the current working directory (the
# repo root when launched via `litres-web` / `litres-mcp` from there), or set
# LITRES_SESSION_FILE to an absolute path to pin it elsewhere. Kept relative
# rather than package-relative so this shared core makes no assumption about
# where either subproject is installed.
SESSION_STATE_PATH = Path(os.environ.get("LITRES_SESSION_FILE", ".litres_session.json"))

_state = {"client": None, "login": None}
# max_workers is intentionally fixed at 1, not configurable: Playwright's
# sync API is tied to whichever single thread created it (see module
# docstring), so more workers would mean a client's calls landing on a
# different thread than the one that created it -- an immediate crash, not
# a performance tuning knob.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="litres-playwright")


def run(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) on the single dedicated Playwright thread and
    block for its result. Use this (or run_async) for *any* call that
    touches a LitresClient -- never call its methods from an arbitrary
    thread."""
    return _executor.submit(fn, *args, **kwargs).result()


def submit(fn, *args, **kwargs):
    """Non-blocking version of run() -- schedules fn on the dedicated
    thread and returns a concurrent.futures.Future immediately."""
    return _executor.submit(fn, *args, **kwargs)


async def run_async(fn, *args, **kwargs):
    """Awaitable version of run(), for async callers (the MCP server)."""
    future = _executor.submit(fn, *args, **kwargs)
    return await asyncio.wrap_future(future)


def current_client() -> Optional[LitresClient]:
    return _state["client"]


def current_login() -> Optional[str]:
    return _state["login"]


def _restore_session_impl(allow_env_login: bool = True) -> None:
    if _state["client"] is not None:
        logger.debug("Session already active, skipping restore")
        return  # already restored/logged in this process

    if SESSION_STATE_PATH.exists():
        client = LitresClient(storage_state_path=SESSION_STATE_PATH)
        if client.is_logged_in():
            saved = credentials.load_last()
            # The keychain entry and the .env login are two independent
            # "remember who this is" sources (see login()/credentials.py) --
            # cookies alone don't carry a login name, so without this
            # fallback a keychain miss would restore a working session that
            # displays no login at all. Only the MCP server consults .env
            # (allow_env_login) -- the web UI never does; see login below.
            _state["client"] = client
            _state["login"] = saved[0] if saved else (os.environ.get("LITRES_LOGIN") if allow_env_login else None)
            logger.info("Restored saved session for %s", _state["login"])
            return
        logger.info("Saved session cookies are no longer valid, discarding")
        client.close()

    saved = credentials.load_last()
    if not saved:
        # No saved cookie session and no keychain credentials. The web UI
        # stops here and shows its login form (allow_env_login=False): the
        # user logs in through the page, which then persists the session +
        # keychain for reuse. Only the headless MCP server -- which has no
        # interactive login form -- falls back to LITRES_LOGIN/LITRES_PASSWORD
        # from the environment (.env) to bootstrap a first session.
        if not allow_env_login:
            logger.info("No saved session or keychain credentials -- staying logged out (env login is MCP-only)")
            return
        env_login, env_password = os.environ.get("LITRES_LOGIN"), os.environ.get("LITRES_PASSWORD")
        if not (env_login and env_password):
            logger.info("No saved session or credentials found -- staying logged out")
            return
        saved = (env_login, env_password)
        logger.info("Bootstrapping session from LITRES_LOGIN/LITRES_PASSWORD")
    login_id, password = saved
    client = LitresClient()
    try:
        client.login(login_id, password)
    except LitresAuthError:
        logger.warning("Automatic login for %s failed", login_id)
        client.close()
        return
    client.save_state(SESSION_STATE_PATH)
    _state["client"], _state["login"] = client, login_id
    logger.info("Logged in as %s and saved session", login_id)


def _login_impl(login_id: str, password: str) -> LitresClient:
    client = LitresClient()
    try:
        client.login(login_id, password)
    except LitresAuthError:
        logger.warning("Login failed for %s", login_id)
        client.close()
        raise
    if _state["client"] is not None:
        _state["client"].close()
    client.save_state(SESSION_STATE_PATH)
    credentials.save(login_id, password)
    _state["client"], _state["login"] = client, login_id
    # A fresh (non-cookie-restore) login may be a different litres.ru
    # account than whatever the cache was last filled from -- cheaper to
    # always drop it here than to risk one account's library/files leaking
    # into another's view.
    cache.clear()
    logger.info("Logged in as %s", login_id)
    return client


def _logout_impl() -> None:
    logger.info("Logging out %s", _state["login"])
    if _state["login"]:
        credentials.forget(_state["login"])
    if _state["client"] is not None:
        _state["client"].close()
    SESSION_STATE_PATH.unlink(missing_ok=True)
    _state["client"], _state["login"] = None, None
    cache.clear()


def _shutdown_impl() -> None:
    if _state["client"] is not None:
        _state["client"].close()
        _state["client"] = None


def restore_session(allow_env_login: bool = True) -> None:
    """Reuse a previously saved browser session (cookies incl. the
    DataDome-style challenge cookies) first, so we don't drive a fresh
    login on every process start.

    `allow_env_login` controls the *last-resort* bootstrap when there's no
    saved session and no keychain credentials: the headless MCP server passes
    True (fall back to LITRES_LOGIN/LITRES_PASSWORD from .env), the web UI
    passes False (stop and show its login form instead). A saved session or
    keychain re-login is used by both regardless."""
    run(_restore_session_impl, allow_env_login)


def login(login_id: str, password: str) -> LitresClient:
    """Log in fresh, persist the session, and make it the active client."""
    return run(_login_impl, login_id, password)


def logout() -> None:
    run(_logout_impl)


def shutdown() -> None:
    """Close the active client without forgetting saved credentials/session."""
    run(_shutdown_impl)
