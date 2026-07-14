"""Test doubles shared across the suite. None of these touch Playwright,
the network, or the real OS keychain -- that's the whole point: they let us
exercise the real orchestration/parsing code in client.py, session.py,
activity.py, web.py, and mcp_server.py against controllable, fast,
offline fakes.
"""
from __future__ import annotations

from litres_core.client import LitresAuthError, LitresClient


class FakeAPIResponse:
    """Stands in for Playwright's APIResponse."""

    def __init__(self, status=200, json_data=None, text_data="", body_data=b""):
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._text = text_data
        self._body_data = body_data

    @property
    def ok(self):
        return 200 <= self.status < 400

    def json(self):
        return self._json

    def text(self):
        return self._text

    def body(self):
        return self._body_data


class FakeRequestContext:
    """Stands in for Playwright's `context.request` (an APIRequestContext).
    `handler(url, params, headers, timeout) -> FakeAPIResponse` decides what
    each call returns; every call is recorded in `.calls` for assertions."""

    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return self._handler(url, params, headers, timeout)


class FakeContext:
    def __init__(self, handler):
        self.request = FakeRequestContext(handler)

    def cookies(self):
        # download_file reads the browser context's cookies to hand to its
        # httpx client -- no cookies needed for the offline (MockTransport) path.
        return []


def make_bare_client(handler, extra_headers=None) -> LitresClient:
    """A real LitresClient with Playwright never started -- `.context` is a
    fake driven by `handler`, so iter_library/get_files/download_file/
    is_logged_in run their real production logic against canned responses."""
    client = LitresClient.__new__(LitresClient)
    client.context = FakeContext(handler)
    client._extra_headers = extra_headers if extra_headers is not None else {"app-id": "115"}
    # download_file streams via httpx; tests set this to an httpx.MockTransport.
    client._httpx_transport = None
    return client


class FakeLitresClient:
    """A full high-level fake for testing orchestration code (activity,
    web routes, session, mcp tools) that doesn't care about HTTP-layer
    details, just the client's public behavior/failure modes."""

    def __init__(self, library=None, files_by_id=None, fail_downloads=None):
        self.library = library if library is not None else []
        self.files_by_id = files_by_id or {}
        # art_ids whose download_file() should raise, simulating a stalled
        # transfer / DDoS-Guard block / any other per-book failure.
        self.fail_downloads = set(fail_downloads or ())
        self.fail_login = False
        self._is_logged_in = True
        self.closed = False
        self.storage_state_path = None
        self.saved_state_path = None
        self.login_calls = []
        self.download_calls = []

    def login(self, login, password):
        self.login_calls.append((login, password))
        if self.fail_login:
            raise LitresAuthError("Login failed (401): Incorrect user data")

    def is_logged_in(self):
        return self._is_logged_in

    def save_state(self, path):
        # Mirror the real LitresClient.save_state's observable effect (a
        # file that then exists on disk) so tests can assert on it.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.saved_state_path = path

    def close(self):
        self.closed = True

    def iter_library(self, limit: int = 100):
        yield from self.library

    def get_files(self, art_id):
        return self.files_by_id.get(art_id, [])

    def pick_best_file(self, files, preferred_ext=None, preferred_file_type=None):
        # Delegate to the real implementation (it doesn't use `self`) so
        # tests exercise production logic, not a reimplementation of it.
        return LitresClient.pick_best_file(self, files, preferred_ext, preferred_file_type)

    @staticmethod
    def file_extension(file_entry):
        return LitresClient.file_extension(file_entry)

    def download_file(self, art_id, release_file_id, filename, dest, subscr=False,
                      should_cancel=None, on_progress=None):
        self.download_calls.append(art_id)
        if art_id in self.fail_downloads:
            raise LitresAuthError(f"Download failed for art {art_id} (403): DDoS-Guard")
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = b"FAKEDATA"
        dest.write_bytes(data)
        if on_progress is not None:
            on_progress(len(data), len(data))
        return dest


def client_factory(monkeypatch, session_module, **kwargs):
    """Monkeypatch session_module.LitresClient so every construction
    returns the same preconfigured FakeLitresClient. Returns that fake."""
    fake = FakeLitresClient(**kwargs)
    monkeypatch.setattr(session_module, "LitresClient", lambda storage_state_path=None: fake)
    return fake


class FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        pass


class FakeKeyring:
    """In-memory stand-in for the `keyring` module used by credentials.py."""

    errors = FakeKeyringErrors

    def __init__(self):
        self._store = {}

    def set_password(self, service, username, password):
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username):
        if (service, username) not in self._store:
            raise self.errors.PasswordDeleteError()
        del self._store[(service, username)]
