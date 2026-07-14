"""Desktop launcher: embed the reused BookVault web app in a native window.

The native GUI toolkit (WKWebView on macOS, WebView2 on Windows, WebKitGTK on
Linux) owns the process's main thread -- Cocoa in particular hard-asserts that
`WKWebView` and the app run loop live on thread 0, the one that ran `main()`.
So the split is: the native webview owns the main thread, and uvicorn -- which
runs its own blocking asyncio loop -- owns a daemon background thread. The
window just points at http://127.0.0.1:<port> served by that background uvicorn.

We reuse `bookvault_web.app:app` verbatim (zero changes to bookvault_web). Its
FastAPI lifespan restores/tears down the Playwright session, so a clean
programmatic shutdown of uvicorn is what stops headless Chromium -- see main().

CI note: `import webview` eagerly resolves a native GUI backend and fails on a
headless runner, so it is imported LAZILY inside main(). Everything else in this
module (`_free_port`, `build_server`, `_wait_until_serving`) stays importable --
and unit-testable -- without a display or a GUI backend.
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import uvicorn

# Safe to import at module top -- the FastAPI app object touches no GUI. Its
# lifespan only launches Playwright/Chromium if there's a saved session or
# keychain credentials to restore; a fresh user stays logged out with no
# browser (see bookvault_core.session.restore_session(allow_env_login=False)).
from bookvault_web.app import app

WINDOW_TITLE = "BookVault"

# A tiny self-contained splash shown the instant the window opens, then swapped
# for the real app once the backend is serving. Returning users have a saved
# session that the FastAPI lifespan restores by launching headless Chromium --
# that can take 10-30s, and we don't want a blank/unresponsive window (or a
# connection-refused page) during it.
_SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{height:100%;margin:0}
  body{display:flex;flex-direction:column;align-items:center;justify-content:center;
       font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#faf7f2;color:#5b5147}
  .dot{width:10px;height:10px;border-radius:50%;background:#a3311f;display:inline-block;
       animation:b 1s infinite ease-in-out both;margin:0 3px}
  .dot:nth-child(2){animation-delay:.15s}.dot:nth-child(3){animation-delay:.3s}
  @keyframes b{0%,80%,100%{opacity:.25}40%{opacity:1}}
  h1{font-weight:600;margin:0 0 .4rem}p{margin:.2rem 0;font-size:.9rem;opacity:.8}
</style></head><body>
  <h1>📚 BookVault</h1>
  <p>Starting up &amp; restoring your session…</p>
  <div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
</body></html>"""


def _free_port() -> int:
    """Ask the OS for an unused localhost port by binding to port 0, reading
    the assigned number back, and releasing it. There's a tiny TOCTOU window
    before uvicorn rebinds it -- acceptable for a single local app, and we need
    the number *before* creating the window, so passing port=0 to uvicorn isn't
    an option here."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_server(port: int) -> uvicorn.Server:
    """Construct the uvicorn Server by hand rather than via `uvicorn.run()`,
    which assumes it owns the main thread and installs signal handlers.

    - host 127.0.0.1: WKWebView is strict about mixed content and localhost, so
      bind (and later load) exactly this origin.
    - lifespan="on": guarantees the FastAPI lifespan *shutdown* phase runs when
      we set should_exit -- that's what closes the Playwright session.
    - timeout_graceful_shutdown=5: bound the drain. Without it uvicorn waits
      *indefinitely* for in-flight requests before running lifespan shutdown --
      and this app has genuinely long-lived responses (streaming a whole-library
      zip via /download/file). If the user closes the window mid-download, an
      unbounded drain would outlast our join, the daemon thread would be killed
      at interpreter exit, and lifespan shutdown (Playwright/Chromium close)
      would never run -- orphaning the browser. Capping the drain lets shutdown
      always reach the teardown.
    - install_signal_handlers neutralized to a no-op: Python only allows
      registering signal handlers on the main thread of the main interpreter,
      and uvicorn will run on a worker thread. Signals aren't needed anyway --
      shutdown is driven programmatically from the window-close handler.
    """
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        lifespan="on",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    return server


def _wait_until_serving(
    server: uvicorn.Server,
    thread: Optional[threading.Thread] = None,
    timeout: float = 10.0,
) -> None:
    """Block until uvicorn's socket is bound and accepting, by polling the real
    `server.started` flag (not a fixed sleep). This closes the race where the
    webview would request the URL before the listener exists and show a
    connection-refused page.

    If the server thread is passed and dies before `started` flips (a bind
    failure on our race-won port, or a lifespan-startup exception), fail fast
    with a clear message instead of spinning the full timeout on a server that
    is never coming up."""
    deadline = time.monotonic() + timeout
    while not server.started:
        if thread is not None and not thread.is_alive():
            raise RuntimeError(
                "the embedded BookVault server thread exited before it began "
                "serving (port bind failure or startup error) -- see the log above"
            )
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn failed to start within timeout")
        time.sleep(0.05)


def main() -> None:
    # LAZY import: keeps this module importable on a headless CI runner where no
    # native GUI backend (pyobjc / WebKitGTK) is available. Only the two lines
    # that actually open a window need it.
    import webview

    port = _free_port()
    server = build_server(port)
    app_url = f"http://127.0.0.1:{port}"

    # server.run() is the synchronous entry point that wraps
    # asyncio.run(server.serve()) and owns its own event loop -- the right
    # thread target (not server.serve, which is a coroutine).
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Open the window immediately with a splash (don't block the main thread
    # waiting for a possibly-slow session restore). We create it with HTML, not
    # the URL, so it never shows a connection-refused page before the server is
    # up.
    window = webview.create_window(
        WINDOW_TITLE,
        html=_SPLASH_HTML,
        width=1100,
        height=800,
        min_size=(900, 600),
    )
    # Closing the window sets should_exit; the close handler must NOT do GUI
    # teardown itself -- the actual resource cleanup lives in the FastAPI
    # lifespan shutdown block so it runs deterministically once uvicorn's loop
    # notices the flag.
    window.events.closed += lambda: setattr(server, "should_exit", True)

    def _load_app_when_ready() -> None:
        # Runs on a pywebview worker thread once the GUI loop is up. Wait for the
        # backend (its lifespan restore can take a while for a returning user),
        # then swap the splash for the real app. Generous timeout because a
        # session restore drives a real headless Chromium.
        try:
            _wait_until_serving(server, thread, timeout=90.0)
            window.load_url(app_url)
        except Exception as exc:  # server never came up -- show why, don't hang blank
            window.load_html(f"<body style='font-family:sans-serif;padding:2rem'>"
                             f"<h2>BookVault couldn't start its backend</h2><pre>{exc}</pre></body>")

    # Blocks on the MAIN thread with the native event loop until the window is
    # closed; `_load_app_when_ready` runs on a worker thread after the loop
    # starts. Returns only after webview shuts down.
    webview.start(_load_app_when_ready)

    # Window closed -> should_exit set -> uvicorn stops accepting, drains, then
    # runs the lifespan shutdown (closing the Playwright session / headless
    # Chromium). Belt-and-suspenders in case the closed handler didn't fire.
    server.should_exit = True
    # Daemon thread would be killed on process exit, but join so the lifespan
    # shutdown actually completes its teardown before we go.
    thread.join(timeout=10)


if __name__ == "__main__":
    main()
