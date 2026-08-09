"""Local, single-user credential storage backed by the OS keychain.

Nothing is written to a plaintext file; `keyring` delegates to macOS
Keychain (or the platform equivalent), so the password only ever lives
where the OS already trusts local apps to store secrets.

A headless Linux container has no OS keychain (no Secret Service / D-Bus),
so `keyring` raises `NoKeyringError`. Credential storage is best-effort: if
there's no backend we log and degrade to session-only. The saved browser
session (cookies, on the mounted volume) still persists a login across
restarts for weeks; only *silent* re-login after that session finally
lapses is unavailable -- the user re-logs-in through the web form instead.
This keeps the container's posture strictly no-secret-at-rest.
"""
from __future__ import annotations

import logging

import keyring
import keyring.errors

logger = logging.getLogger(__name__)

SERVICE_NAME = "bookvault"
_LAST_LOGIN_KEY = "_last_login"


def _no_backend_errors():
    """Exception types meaning 'no usable keyring backend here' (e.g. a
    headless container). Resolved through the `keyring` module at call time so
    tests can substitute a fake keyring and its error classes."""
    return (keyring.errors.NoKeyringError, keyring.errors.KeyringError)


def save(login: str, password: str) -> None:
    try:
        keyring.set_password(SERVICE_NAME, login, password)
        keyring.set_password(SERVICE_NAME, _LAST_LOGIN_KEY, login)
    except _no_backend_errors() as exc:
        logger.info("No OS keyring available -- not persisting credentials (session-only): %s", exc)


def load_last() -> tuple[str, str] | None:
    try:
        login = keyring.get_password(SERVICE_NAME, _LAST_LOGIN_KEY)
        if not login:
            return None
        password = keyring.get_password(SERVICE_NAME, login)
    except _no_backend_errors() as exc:
        logger.debug("No OS keyring available -- no saved credentials to load: %s", exc)
        return None
    if not password:
        return None
    return login, password


def forget(login: str) -> None:
    for username in (login, _LAST_LOGIN_KEY):
        try:
            keyring.delete_password(SERVICE_NAME, username)
        except (keyring.errors.PasswordDeleteError, *_no_backend_errors()):
            pass
