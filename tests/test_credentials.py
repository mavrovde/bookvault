"""Tests for bookvault_core/credentials.py against the fake in-memory keyring (see
conftest.py's autouse fake_keyring fixture -- the real OS keychain is never
touched by this suite)."""
from __future__ import annotations

from bookvault_core import credentials

from tests.fakes import NoBackendKeyring


def test_save_then_load_last_roundtrips():
    credentials.save("user@example.com", "hunter2")
    assert credentials.load_last() == ("user@example.com", "hunter2")


def test_load_last_returns_none_when_nothing_saved():
    assert credentials.load_last() is None


def test_load_last_returns_none_if_password_missing_for_pointer(fake_keyring):
    # Simulate a corrupted/partial keychain state: the "last login" pointer
    # exists but the actual password entry doesn't.
    fake_keyring.set_password(credentials.SERVICE_NAME, credentials._LAST_LOGIN_KEY, "user@example.com")
    assert credentials.load_last() is None


def test_save_overwrites_previous_last_login_pointer():
    credentials.save("first@example.com", "pw1")
    credentials.save("second@example.com", "pw2")
    assert credentials.load_last() == ("second@example.com", "pw2")


def test_forget_removes_saved_login_and_pointer():
    credentials.save("user@example.com", "hunter2")
    credentials.forget("user@example.com")
    assert credentials.load_last() is None


def test_forget_when_nothing_saved_does_not_raise():
    credentials.forget("nobody@example.com")  # must not raise


def test_no_keyring_backend_degrades_to_session_only(monkeypatch):
    # A headless container has no OS keychain: keyring raises NoKeyringError.
    # save/load_last/forget must degrade gracefully (no crash), so the web
    # login flow still works -- it just doesn't persist the password.
    monkeypatch.setattr(credentials, "keyring", NoBackendKeyring())
    credentials.save("user@example.com", "hunter2")  # must not raise
    assert credentials.load_last() is None  # nothing persisted, but no crash
    credentials.forget("user@example.com")  # must not raise
