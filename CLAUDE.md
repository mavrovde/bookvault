# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BookVault backs up a user's **own** purchased litres.ru library (books + audiobooks) entirely from their machine. It ships as three front-ends over one shared backend:

- **web** (`bookvault-web`) — a local, single-user FastAPI app at `127.0.0.1:8420` (port via `LITRES_APP_PORT`).
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
.venv/bin/python -m pytest -m ui                # opt-in: browser layer, needs `playwright install chromium`
.venv/bin/ruff check .                          # lint (CI-enforced)

.venv/bin/bookvault-web        # run the web app
.venv/bin/bookvault-mcp        # run the MCP server (stdio)
.venv/bin/bookvault-desktop    # run the desktop app (needs -e ./desktop + pywebview)

docker compose up -d --build   # web + mcp in containers, published to 127.0.0.1 only
packaging/macos/build.sh          # build the macOS .app + .dmg (PyInstaller)
packaging/linux/build-appimage.sh # build the Linux .AppImage (PyInstaller + appimagetool)
# Windows Setup.exe is built in CI (PyInstaller onedir + Inno Setup, packaging/windows/BookVault.iss)
```

Live tests (`tests/test_smoke_live.py`, marker `live`) are **deselected by default** via `addopts = -m "not live"`; run them with `-m live` against a started app (`BOOKVAULT_BASE_URL` overrides the target).

## Architecture — the load-bearing decisions

**One dedicated Playwright worker thread.** Playwright's *sync* API is bound to the thread that created it, so `core/bookvault_core/session.py` funnels **every** call touching a `LitresClient` through a single dedicated worker thread (`session.run`/`submit`). This is the central constraint: there is exactly one logged-in account and one browser at a time. Respect it — never call the client off that thread.

**Why a real browser at all.** litres.ru rejects scripted logins (DataDome/DDoS-Guard anti-bot). `client.py` drives a real headless Chromium through the login form, then captures the app-level headers (`app-id`, `session-id`, …) that the site's own JS attaches, and replays them on API calls. **Downloads** stream over a separate `curl_cffi` client impersonating Chrome so their TLS/JA3 fingerprint matches the browser session (falls back to `httpx`). Transient 403/429/503 with a DDoS-Guard signature are retried with backoff; a bare 403 is a genuine rights-limited title and is *not* retried.

**One backend state machine.** `web/bookvault_web/activity.py` is a single module-level state machine (`idle → refreshing / checking / preparing / stopping → idle`); only one activity runs at a time (falls out of the single worker thread). The browser is a thin renderer: it POSTs an action and polls `GET /activity`. A finished build's per-book results and its zip link are kept **durably** (`results`, `zip_path`, `saved_path`, untouched by `_begin`) so they survive the size-check that fires on the next page load.

**The zip is staged in temp, then moved.** A build always writes into its own `mkdtemp` workdir and the finished archive is moved into the user's save folder (`prefs.resolve_download_dir()`, passed in by `app.py` — `activity.py` never imports `prefs`) only *after* it succeeds, so a crashed or empty build never leaves a half-written `.zip` in someone's Downloads. A failed move keeps the archive in temp and still serves it; the archive is never discarded. Cleanup tracks `_state["workdir"]` explicitly — **never** derive the temp dir from `Path(zip_path).parent`, because once saved that parent is the *user's own folder*.

**Server-side shared UI state.** Selection, format prefs + the save folder live in `web/bookvault_web/prefs.py` (`GET`/`POST /prefs`, and folded into the `/activity` poll), not per-browser — so every browser/tab shows the same view. Persisted to `LITRES_STATE_FILE`. Adding a pref means touching five places: `_DEFAULTS`/`snapshot()`/`update()` in `prefs.py`, `PrefsUpdate` + `set_prefs` in `app.py`, the control in `templates/index.html`, its handler in `static/js/app.js`, and the `_reset()` in `tests/conftest.py`. In `update()`, `None` means "leave alone", so a *clearable* field needs a separate sentinel (`download_dir=""` resets it to the default). Fields *derived* on read (`download_dir_effective`, `download_dir_warning`) are computed in `snapshot()`, not stored — so they notice a folder that has since been deleted or unmounted.

**Cross-origin writes are refused.** The app binds 127.0.0.1 with no auth and no CSRF token by design, so any page the user has open could otherwise POST to it (start a build, fire a sweep, log them out). `block_cross_origin_writes` in `app.py` rejects state-changing verbs whose `Sec-Fetch-Site` isn't `same-origin`/`none`, falling back to an `Origin`-vs-`Host` comparison for engines that don't send it (the Linux desktop's WebKitGTK). Callers with *neither* header — curl, the live tests — are allowed: the threat is a web page, not a local script. **Adding a POST means adding it to `WRITE_ROUTES` in `tests/test_csrf.py`**, which asserts the list is exhaustive and will fail if you don't.

**Sizes are durable; byte progress is not.** `sizes` survives `_begin` alongside `results`/`zip_path`/`saved_path` — it is derived from the 7-day file-listing cache, so an operation starting must not blank it (`forget_sizes()` clears it on logout, since entries are keyed by art_id). `bytes_done`/`bytes_total` are the opposite: real per-build progress, reset by `_begin`. `bytes_total` is a cache-only *estimate* (summing `pick_best_file` for books whose listing is cached) and the UI marks it `~`; the bar still tracks book count, because a denominator that grows mid-run makes it run backwards. Size resolution reads `cache.get_library() or cache.get_library_stale()` — the listing's 15-minute TTL must never govern sizes, which change never.

**The save folder is picked by a native dialog, opened server-side.** A browser cannot hand back a real filesystem path (`webkitdirectory`/`showDirectoryPicker()` both hide it), but the server *is* the user's machine here — so `folder_dialog.py` shells out to `osascript`/PowerShell/`zenity`/`kdialog`. The start path always travels as an argv element or an env var, never interpolated into a script body. The picked path is still validated by `prefs.update()` like a typed one: picking is not a way around the allowed-roots guard. `is_available()` is false in Docker/SSH, which hides the button. It must **not** run on the Playwright worker thread — a dialog waits on a human and would stall any running download.

**Desktop reuses the web app, it does not fork it.** `desktop/bookvault_desktop/app.py` does `from bookvault_web.app import app`, runs it on a background uvicorn thread on a private port, and points a native window at it. The backend starts/stops with the window (bounded graceful shutdown so Playwright/Chromium is never orphaned). Keep `core`/`web`/`mcp` unchanged when working on desktop.

## Longer-form guidance

This file is the short summary. `.claude/` holds the detail, composed in
layers: `common/engineering.md` + `common/collaboration.md` (portable to any
repo) as the baseline, `project/invariants.md` (this codebase, and it
overrides the baseline) on top, with `agents/` and `skills/` building on both.
See `.claude/README.md`.

## Conventions that will trip you up

- **`LITRES_*` env vars and the `Litres*` names are intentional.** The project was renamed litres-assistant → bookvault, but the litres.ru *service* references (env var prefix, `LitresClient`, URLs, `.litres_*` data files) were deliberately kept as nominative references. Don't "fix" them to `BOOKVAULT_*`.
- **Web app never auto-logs-in from `.env`.** `restore_session(allow_env_login=False)` for the web/desktop lifespan; only the headless MCP server reads `LITRES_LOGIN`/`LITRES_PASSWORD`. A fresh session with no saved cookies/keychain **stays logged out and launches no browser** — which is what keeps tests (and the desktop boot) offline.
- **Tests are fully mocked and offline.** `tests/conftest.py` has autouse fixtures that fake the keyring, redirect session/cache/state files to a tmp dir, and reset the module-level singletons in `session`/`activity`/`cache`/`prefs`. `tests/fakes.py` provides `client_factory` (a `FakeLitresClient`) and `make_bare_client` (real client logic against canned `FakeAPIResponse`s). No test starts a real browser or hits the network. New desktop tests guard with `pytest.importorskip("bookvault_desktop")` so the released web/MCP CI (which doesn't install desktop) skips them.
- **Secrets are git-ignored, never committed:** `.env`, `.litres_session.json`, `.litres_cache.json`, `.litres_state.json`. Treat the session file like being logged in.

## Layout

Each subproject has its own `pyproject.toml` and depends on `bookvault-core`:

```
core/bookvault_core/   client.py (login/API/download) · session.py (worker thread) · credentials.py (keyring) · cache.py
web/bookvault_web/     app.py (FastAPI) · activity.py (state machine) · prefs.py (shared UI state) · folder_dialog.py (native save-folder picker) · run.py · templates/ static/
mcp/bookvault_mcp/     server.py (MCP tools)
desktop/bookvault_desktop/  app.py (pywebview launcher; embeds bookvault_web)
packaging/             entry.py (shared frozen-app entry: per-OS data dir + PLAYWRIGHT_BROWSERS_PATH)
                       macos/ (.app/.dmg) · windows/ (Setup.exe via Inno Setup) · linux/ (.AppImage)
tests/                 pytest suite (offline) + test_smoke_live.py (opt-in -m live) + test_ui.py (opt-in -m ui, browser layer)
```

## Releasing

Versions are bumped **in lockstep** across all five `pyproject.toml` files (root + core/web/mcp/desktop); current release is **v1.3.4**. A pushed `v*` tag triggers `.github/workflows/docker-publish.yml`, which builds and publishes the multi-arch `ghcr.io/mavrovde/bookvault/{web,mcp}` images. `.github/workflows/lint-test-audit.yml` runs ruff + the pytest matrix (3.11–3.13) + a dependency audit on every push/PR.

The three desktop installers each build in their own workflow on a matching runner and attach the artifact to the GitHub Release (all unsigned dev builds; Chromium is fetched on first run via `packaging/entry.py`):

- **macOS** `.app`/`.dmg` — `desktop-macos.yml` (`packaging/macos/build.sh`).
- **Windows** `Setup.exe` — `desktop-windows.yml` (PyInstaller onedir + Inno Setup, `packaging/windows/BookVault.iss`). Needs the WebView2 runtime.
- **Linux** `.AppImage` — `desktop-linux.yml` (`packaging/linux/build-appimage.sh`, headless xvfb smoke test). WebKitGTK 4.1 is a **host** runtime dependency (can't be bundled).

**Supply chain.** All four publish workflows record a signed build provenance attestation (`actions/attest-build-provenance`) before uploading, so `gh attestation verify <file> --repo mavrovde/bookvault` proves an artifact came from this repo's CI. Docker uses `subject-digest` with `push-to-registry: true` (the attestation travels with the image); the installers use `subject-path`, gated to tag builds. A fifth workflow, `release-checksums.yml`, waits for all three installers to be attached and then publishes a combined `SHA256SUMS`. Adding a release artifact means updating the `patterns` array there, or the wait loop times out. Provenance is *not* code signing — the builds stay unsigned and Gatekeeper/SmartScreen still warn.
