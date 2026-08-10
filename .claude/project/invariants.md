# BookVault invariants (project-specific)

The things a fresh context cannot infer and will otherwise get wrong. These
override anything in `../common/` where they disagree.

Ordered by how often they get violated.

## 1. One dedicated Playwright worker thread

Playwright's *sync* API is bound to the thread that created it, so
`core/bookvault_core/session.py` funnels **every** call touching a
`LitresClient` through one worker (`session.run` / `submit` / `run_async`).

There is exactly one logged-in account and one browser at a time. Never call
the client from a route handler, a background thread, or a test body. Code
already running *inside* a submission must not submit again — the single worker
would wait on itself and deadlock (see the MCP server's threading note).

Everything else follows from this: only one activity can run at a time, which
is why `activity.py` is a state machine rather than a job queue.

## 2. Request cadence, not volume

litres.ru sits behind an anti-bot layer that keys on **request count and
rhythm**. This is why the on-load size sweep is cache-only, why `iter_library`
jitters between pages, and why autosync runs on a multi-hour interval with
jitter rather than a tidy timer.2push 

Treat as suspect: anything adding requests per book, lowering a page size,
polling on a fixed period, or scanning the whole library to find one item.
A larger response is cheaper than more responses.

A **bare 403 is a rights-limited title** — surface it as a skip. A 403/429/503
carrying a DDoS-Guard signature is transient — retry with backoff. Conflating
them either hammers the service or silently drops books the user owns.

## 3. Tests are offline and fully mocked

No test may start a browser or touch the network. `tests/conftest.py` fakes the
keyring, redirects state/cache/session files into a `tmp_path`, and resets
module-level singletons around every test.

Any new on-disk path — or a default pointing at a real user directory — must be
redirected there, or the suite writes into the developer's own files.

Env vars are read **at import** into module-level constants, so tests
monkeypatch the attribute, not the environment.

## 4. Durable activity state

`results`, `zip_path` and `saved_path` deliberately survive `_begin()`, so a
finished build's results view and download link outlive the size-check that
fires on the next page load. Adding state means deciding explicitly which side
of that line it is on, and saying so in a comment.

The zip is staged in a temp workdir and moved to its destination only on
success. Cleanup tracks `_state["workdir"]` — **never** derive the temp dir
from `Path(zip_path).parent`, because once saved that parent is the user's own
folder.

## 5. Naming is deliberate

`LITRES_*` environment variables, the `Litres*` class names and the
`.litres_*` data files are nominative references to the *service*, kept through
the litres-assistant → bookvault rename. Do not "fix" them to `BOOKVAULT_*`.

## 6. Secrets

`.env`, `.litres_session.json`, `.litres_cache.json` and `.litres_state.json`
are git-ignored and never committed, logged, or pasted into an issue. **Treat
the session file as being logged in** — it is a bearer credential. Never log a
password, a cookie, or the captured app-level headers.

## 7. Local by design, which is also a threat model

The web app binds `127.0.0.1` with no authentication and no login,
deliberately — it is a single-user tool. The consequence was that **any page
the user has open could POST to it**. Since 1.3.3 `block_cross_origin_writes`
(`app.py`) refuses state-changing verbs that a foreign page made, using
`Sec-Fetch-Site` with an `Origin`-vs-`Host` fallback; callers with neither
header (curl, the live tests, local scripts) are still allowed, because the
threat model is a *web page the user visited*, not code already running as
them.

That check is a floor, not a substitute for the rest: keep user-supplied paths
**constrained** rather than merely normalised, because a bug in the check
should not be the only thing standing between a POST and the filesystem.

**Adding a state-changing route means adding it to `WRITE_ROUTES` in
`tests/test_csrf.py`.** That list is asserted to be exhaustive against the
app's own routing table, so a forgotten route fails the suite rather than
quietly becoming reachable from any page.

## 8. Scope

This tool downloads only what the account owner has already purchased, using
their own authenticated session. Anything that would reach other content is out
of scope and must be refused.

## 9. Release mechanics

Versions bump in lockstep across **all five** `pyproject.toml` files (root,
core, web, mcp, desktop). A pushed `v*` tag publishes the Docker images and all
three desktop installers. All builds are unsigned; do not claim otherwise.

## 10. No route may answer with a bare 500

A failure the user can act on must reach them as a readable page or a clean
JSON error, never Starlette's plain-text "Internal Server Error" — which loses
the form they were filling in and tells them nothing. Two rules follow:

- **Convert infrastructure failures into typed errors at the layer that knows
  what they mean.** `LitresClient.__init__` raises `LitresBrowserUnavailable`
  (a `LitresAuthError` subclass, so existing callers already degrade to
  "logged out") rather than letting Playwright's driver message escape.
- **Every route that can fail has a catch-all** that logs the traceback with
  `logger.exception` and returns a fixed, non-derived message. Nothing taken
  from an exception goes into a response body — that is how filesystem paths
  and stack traces leak (CodeQL `py/stack-trace-exposure`).

Unattended startup paths (`restore_session`) must degrade to "logged out"
rather than crash the boot: build the client **inside** the guard, or the app
cannot even show the login form explaining what went wrong.

## 11. A subprocess does not inherit the suite's fakes

`tests/conftest.py`'s autouse fixtures patch *this* process. A test that spawns
a real server (`test_e2e_smoke.py`) gets a fresh interpreter with a real
`keyring`, so on a machine that has ever signed in it will silently restore
that session and test the wrong page. Spawned processes must pin
`PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` alongside the
`LITRES_*_FILE` overrides. A test that only passes on a machine that has never
logged in is not offline — it is lucky.

## 12. The browser cannot give you a filesystem path

`<input webkitdirectory>` exposes relative names and `showDirectoryPicker()` a
sandboxed handle; neither yields an absolute path, by design. Anything needing
a real directory is picked by a **native dialog opened server-side**
(`folder_dialog.py`) — legitimate here only because the server runs on the
user's own machine. When shelling out to one, the start path travels as an
argv element or an environment variable, never interpolated into a script
body, and the result is re-validated by the same guard a typed path goes
through.
