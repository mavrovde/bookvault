"""Tests for the desktop launcher's non-GUI helpers in bookvault_desktop/app.py.

The whole module is skipped unless the desktop subproject is installed: the
released web/mcp CI matrix never runs `pip install -e ./desktop`, so this file
is collected as skipped there and leaves the existing suite untouched. Locally
(or in a future desktop CI job) it imports the launcher and exercises the
free-port picker, `build_server`, and the readiness poll against a REAL uvicorn
Server on a background thread -- without ever importing `webview` or opening a
window. The autouse fixtures in conftest.py keep this offline: fake keychain,
no env creds, session file redirected to a tmp path -> the reused
bookvault_web app boots logged-out, so GET / is served with no browser."""
from __future__ import annotations

import pytest

pytest.importorskip("bookvault_desktop")

import socket
import threading
import urllib.request

from bookvault_desktop import app as desktop
from bookvault_web.app import app as web_app


def test_free_port_returns_a_bindable_int():
    port = desktop._free_port()
    assert isinstance(port, int)
    assert 0 < port < 65536
    # The port the OS handed us is actually free: we can bind it right now.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))


def test_build_server_wraps_the_reused_web_app():
    server = desktop.build_server(8420)
    # It's the real reused bookvault_web app, bound localhost-only on our port.
    assert server.config.app is web_app
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8420
    # lifespan="on" so the FastAPI shutdown phase (Playwright teardown) runs.
    assert server.config.lifespan == "on"
    # Signal handlers are neutralized to a no-op -- uvicorn will run off the
    # main thread, where signal.signal() would raise ValueError.
    server.install_signal_handlers()  # must not raise


def test_server_serves_the_logged_out_app_and_shuts_down():
    """End-to-end (minus the window): run build_server on a background thread,
    wait until it's really accepting, GET / over HTTP, then drive the same
    programmatic shutdown the window-close handler triggers. The fresh app
    stays logged out (no Playwright/Chromium), so this is safe and offline."""
    port = desktop._free_port()
    server = desktop.build_server(port)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        desktop._wait_until_serving(server, timeout=10.0)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
    finally:
        # Same shutdown path as the closed-window handler: flag it, then join
        # so the lifespan shutdown completes.
        server.should_exit = True
        thread.join(timeout=10)
    assert not thread.is_alive()
