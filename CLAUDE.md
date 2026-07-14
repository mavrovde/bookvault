# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BookVault backs up a user's **own** purchased litres.ru library (books + audiobooks) entirely from their machine. It ships as three front-ends over one shared backend:

- **web** (`bookvault-web`) — a local, single-user FastAPI app at `127.0.0.1:8420`.
- **mcp** (`bookvault-mcp`) — an MCP server exposing the library as tools.
- **desktop** (`bookvault-desktop`) — a pywebview native window that **embeds the web app verbatim**.

## Commands

```bash
# Dev install (editable) + tooling. Add -e ./desktop for the desktop app.
.venv/bin/pip install -e ./core -e ./web -e ./mcp -e ".[dev]"
.venv/bin/playwright install chromium          # one-time browser download

.venv/bin/python -m pytest                      # full suite (offline, ~seconds)
.venv/bin/python -m pytest tests/test_web.py::test_login_success_redirects_home  # one test
.venv/bin/python -m pytest -m live              # opt-in: hits a RUNNING server (see below)
.venv/bin/ruff check .                          # lint (CI-enforced)

.venv/bin/bookvault-web        # run the web app
.venv/bin/bookvault-mcp        # run the MCP server (stdio)
.venv/bin/bookvault-desktop    # run the desktop app (needs -e ./desktop + pywebview)

docker compose up -d --build   # web + mcp in containers, published to 127.0.0.1 only
packaging/macos/build.sh       # build the macOS .app + .dmg (PyInstaller)
```

Live tests (`tests/test_smoke_live.py`, marker `live`) are **deselected by default** via `addopts = -m "not live"`; run them with `-m live` against a started app (`BOOKVAULT_BASE_URL` overrides the target).

## Architecture — the load-bearing decisions

**One dedicated Playwright worker thread.** Playwright's *sync* API is bound to the thread that created it, so `core/bookvault_core/session.py` funnels **every** call touching a `LitresClient` through a single dedicated worker thread (`session.run`/`submit`). This is the central constraint: there is exactly one logged-in account and one browser at a time. Respect it — never call the client off that thread.

**Why a real browser at all.** litres.ru rejects scripted logins (DataDome/DDoS-Guard anti-bot). `client.py` drives a real headless Chromium through the login form, then captures the app-level headers (`app-id`, `session-id`, …) that the site's own JS attaches, and replays them on API calls. **Downloads** stream over a separate `curl_cffi` client impersonating Chrome so their TLS/JA3 fingerprint matches the browser session (falls back to `httpx`). Transient 403/429/503 with a DDoS-Guard signature are retried with backoff; a bare 403 is a genuine rights-limited title and is *not* retried.

**One backend state machine.** `web/bookvault_web/activity.py` is a single module-level state machine (`idle → refreshing / checking / preparing / stopping → idle`); only one activity runs at a time (falls out of the single worker thread). The browser is a thin renderer: it POSTs an action and polls `GET /activity`. A finished build's per-book results and its zip link are kept **durably** (`results`, `zip_path`, untouched by `_begin`) so they survive the size-check that fires on the next page load.

**Server-side shared UI state.** Selection + format prefs live in `web/bookvault_web/prefs.py` (`GET`/`POST /prefs`, and folded into the `/activity` poll), not per-browser — so every browser/tab shows the same view. Persisted to `LITRES_STATE_FILE`.

**Desktop reuses the web app, it does not fork it.** `desktop/bookvault_desktop/app.py` does `from bookvault_web.app import app`, runs it on a background uvicorn thread on a private port, and points a native window at it. The backend starts/stops with the window (bounded graceful shutdown so Playwright/Chromium is never orphaned). Keep `core`/`web`/`mcp` unchanged when working on desktop.

## Conventions that will trip you up

- **`LITRES_*` env vars and the `Litres*` names are intentional.** The project was renamed litres-assistant → bookvault, but the litres.ru *service* references (env var prefix, `LitresClient`, URLs, `.litres_*` data files) were deliberately kept as nominative references. Don't "fix" them to `BOOKVAULT_*`.
- **Web app never auto-logs-in from `.env`.** `restore_session(allow_env_login=False)` for the web/desktop lifespan; only the headless MCP server reads `LITRES_LOGIN`/`LITRES_PASSWORD`. A fresh session with no saved cookies/keychain **stays logged out and launches no browser** — which is what keeps tests (and the desktop boot) offline.
- **Tests are fully mocked and offline.** `tests/conftest.py` has autouse fixtures that fake the keyring, redirect session/cache/state files to a tmp dir, and reset the module-level singletons in `session`/`activity`/`cache`/`prefs`. `tests/fakes.py` provides `client_factory` (a `FakeLitresClient`) and `make_bare_client` (real client logic against canned `FakeAPIResponse`s). No test starts a real browser or hits the network. New desktop tests guard with `pytest.importorskip("bookvault_desktop")` so the released web/MCP CI (which doesn't install desktop) skips them.
- **Secrets are git-ignored, never committed:** `.env`, `.litres_session.json`, `.litres_cache.json`, `.litres_state.json`. Treat the session file like being logged in.

## Layout

Each subproject has its own `pyproject.toml` and depends on `bookvault-core`:

```
core/bookvault_core/   client.py (login/API/download) · session.py (worker thread) · credentials.py (keyring) · cache.py
web/bookvault_web/     app.py (FastAPI) · activity.py (state machine) · prefs.py (shared UI state) · run.py · templates/ static/
mcp/bookvault_mcp/     server.py (MCP tools)
desktop/bookvault_desktop/  app.py (pywebview launcher; embeds bookvault_web)
packaging/macos/       PyInstaller spec + build.sh for the .app/.dmg
tests/                 pytest suite (offline) + tests/test_smoke_live.py (opt-in -m live)
```

## Releasing

Versions are bumped **in lockstep** across all `pyproject.toml` files. A pushed `v*` tag triggers `.github/workflows/docker-publish.yml`, which builds and publishes the multi-arch `ghcr.io/mavrovde/bookvault/{web,mcp}` images. `.github/workflows/lint-test-audit.yml` runs ruff + the pytest matrix (3.11–3.13) + a dependency audit on every push/PR. The macOS desktop `.dmg` is built and attached to the GitHub Release separately (unsigned dev build; Chromium is fetched on first run).
