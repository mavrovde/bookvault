"""Local, single-user credential storage backed by the OS keychain.

Nothing is written to a plaintext file; `keyring` delegates to macOS
Keychain (or the platform equivalent), so the password only ever lives
where the OS already trusts local apps to store secrets.
"""
from __future__ import annotations

from typing import Optional, Tuple

import keyring
import keyring.errors

SERVICE_NAME = "litres-assistant"
_LAST_LOGIN_KEY = "_last_login"


def save(login: str, password: str) -> None:
    keyring.set_password(SERVICE_NAME, login, password)
    keyring.set_password(SERVICE_NAME, _LAST_LOGIN_KEY, login)


def load_last() -> Optional[Tuple[str, str]]:
    login = keyring.get_password(SERVICE_NAME, _LAST_LOGIN_KEY)
    if not login:
        return None
    password = keyring.get_password(SERVICE_NAME, login)
    if not password:
        return None
    return login, password


def forget(login: str) -> None:
    try:
        keyring.delete_password(SERVICE_NAME, login)
    except keyring.errors.PasswordDeleteError:
        pass
    try:
        keyring.delete_password(SERVICE_NAME, _LAST_LOGIN_KEY)
    except keyring.errors.PasswordDeleteError:
        pass
