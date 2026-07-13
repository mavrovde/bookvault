"""Shared login/session-restore logic for both the web UI and the MCP server.

Both entry points need the same "restore a saved session, or bootstrap
from .env, or fail" dance -- kept in one place so they can't drift.

Playwright's sync API is tied to whichever single thread created it -- every
call touching a `LitresClient` (this module's own login/restore code, but
also `web.py`'s /library route and `download_job.py`'s background job) MUST
run on that same thread or it fails with "Cannot switch to a different
thread". `run`/`run_async` below are the one gateway to that dedicated
thread; nothing else should call a LitresClient method directly.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import credentials
from .client import LitresAuthError, LitresClient

SESSION_STATE_PATH = Path(__file__).parent.parent / ".litres_session.json"

_state = {"client": None, "login": None}
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


def _restore_session_impl() -> None:
    if _state["client"] is not None:
        return  # already restored/logged in this process

    if SESSION_STATE_PATH.exists():
        client = LitresClient(storage_state_path=SESSION_STATE_PATH)
        if client.is_logged_in():
            saved = credentials.load_last()
            _state["client"] = client
            _state["login"] = saved[0] if saved else None
            return
        client.close()

    saved = credentials.load_last()
    if not saved:
        env_login, env_password = os.environ.get("LITRES_LOGIN"), os.environ.get("LITRES_PASSWORD")
        if not (env_login and env_password):
            return
        saved = (env_login, env_password)
    login_id, password = saved
    client = LitresClient()
    try:
        client.login(login_id, password)
    except LitresAuthError:
        client.close()
        return
    client.save_state(SESSION_STATE_PATH)
    _state["client"], _state["login"] = client, login_id


def _login_impl(login_id: str, password: str) -> LitresClient:
    client = LitresClient()
    try:
        client.login(login_id, password)
    except LitresAuthError:
        client.close()
        raise
    if _state["client"] is not None:
        _state["client"].close()
    client.save_state(SESSION_STATE_PATH)
    credentials.save(login_id, password)
    _state["client"], _state["login"] = client, login_id
    return client


def _logout_impl() -> None:
    if _state["login"]:
        credentials.forget(_state["login"])
    if _state["client"] is not None:
        _state["client"].close()
    SESSION_STATE_PATH.unlink(missing_ok=True)
    _state["client"], _state["login"] = None, None


def _shutdown_impl() -> None:
    if _state["client"] is not None:
        _state["client"].close()
        _state["client"] = None


def restore_session() -> None:
    """Reuse a previously saved browser session (cookies incl. the
    DataDome-style challenge cookies) first, so we don't drive a fresh
    login on every process start."""
    run(_restore_session_impl)


def login(login_id: str, password: str) -> LitresClient:
    """Log in fresh, persist the session, and make it the active client."""
    return run(_login_impl, login_id, password)


def logout() -> None:
    run(_logout_impl)


def shutdown() -> None:
    """Close the active client without forgetting saved credentials/session."""
    run(_shutdown_impl)
