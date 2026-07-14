# Changelog

## [1.2.0] - Linux installer (AppImage)

Adds a Linux **AppImage**, completing the desktop trio (macOS / Windows / Linux).
Built with PyInstaller + `appimagetool` on an `ubuntu-latest` runner, with a
headless smoke test (xvfb) that boots the frozen app to prove the GTK/WebKit
binding works, then attaches the `.AppImage` to each release.

### Added
- **Linux `.AppImage`.** `chmod +x` and run it. It bundles the app + the gi
  bindings/typelib; **WebKitGTK 4.1 is a host runtime dependency** (WebKit's
  multiprocess helpers use compile-time absolute paths that can't live inside an
  AppImage) — one `apt` line covers it on Ubuntu 24.04+
  (`libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1 gir1.2-gtk-3.0`). Chromium is fetched
  on first run; app data lives in `~/.local/share/BookVault/`.
- CI (`.github/workflows/desktop-linux.yml`) builds + smoke-tests the AppImage on
  `ubuntu-latest` on every tag and attaches it to the GitHub Release.

## [1.1.0] - Windows installer (Setup.exe)

Adds a native **Windows** installer, built the same way as macOS: a PyInstaller
onedir app wrapped by Inno Setup into `Setup.exe`, with Chromium fetched on first
run. Built on a `windows-latest` CI runner and attached to each release.

### Added
- **Windows `Setup.exe`.** Download it from the release and run it (unsigned, so
  SmartScreen → *More info → Run anyway*); BookVault installs to Program Files
  with a Start-menu shortcut. Requires the WebView2 runtime (present on Windows
  11 and up-to-date Windows 10). App data lives in `%LOCALAPPDATA%\BookVault`;
  Chromium caches in `%LOCALAPPDATA%\ms-playwright` (shared, survives reinstall).
- CI (`.github/workflows/desktop-windows.yml`) builds `Setup.exe` on
  `windows-latest` on every tag and attaches it to the GitHub Release, and runs
  as a build check on PRs touching the Windows packaging.

## [1.0.1] - Fix: the packaged macOS app couldn't launch its browser

The downloadable macOS app failed at login with an "Internal server error".
Inside a frozen `.app`, Playwright resolved Chromium to a path *inside* the
read-only bundle and could neither install nor launch it, so every login died at
`chromium.launch()`. Fixed by pinning the browser cache (and the app's data dir)
to the standard writable per-OS locations.

### Fixed
- **Desktop app login on the packaged macOS build.** `chromium.launch()` failed
  with `Executable doesn't exist at …/BookVault.app/…/.local-browsers/…` because
  a frozen Playwright defaults its browsers path relative to the (read-only)
  bundle. The launcher now sets `PLAYWRIGHT_BROWSERS_PATH` to the standard
  `~/Library/Caches/ms-playwright` cache — writable, persistent, and shared with
  any normal Playwright install on the machine.

### Changed
- The frozen entry point moved to a single cross-platform `packaging/entry.py`
  (per-user data dir + browsers path chosen by OS), shared by all packaged
  builds.

## [1.0.0] - BookVault 1.0: now a native desktop app, too

BookVault reaches 1.0 with a third way to run it: a **native desktop app** for
macOS, Windows, and Linux, alongside the local web app and the MCP server. All
three are front-ends over the same backend -- the desktop app embeds the exact
`bookvault-web` server and shows it in a native window, with no browser tab or
terminal needed.

### Added
- **`bookvault-desktop` -- a native desktop app.** A
  [pywebview](https://pywebview.flowrl.com/) shell that starts the FastAPI web
  app on a private `127.0.0.1` port in a background thread and shows it in a
  native OS window (WKWebView on macOS, WebView2 on Windows, WebKitGTK on
  Linux). It **reuses `bookvault-web` verbatim** -- the backend is imported, not
  duplicated -- so the web app, the MCP server, and their Docker images are
  unchanged. A splash is shown while a saved session restores (which drives a
  real headless Chromium), then the live app loads; closing the window cleanly
  stops the backend and the Playwright/Chromium session (bounded graceful
  shutdown, so the browser is never orphaned).

  ```bash
  .venv/bin/pip install -e ./core -e ./web -e ./desktop
  .venv/bin/bookvault-desktop
  ```

### Notes
- The desktop app currently runs from a source checkout; **packaged installers**
  (`.dmg` / `.msi` / `.AppImage`) and package-manager channels (Homebrew /
  winget / AUR) are the next step.
- `tests/test_desktop.py` skips itself unless the `desktop` package is
  installed, so the released web/MCP CI is unaffected.

## [0.12.0] - Shared state across browsers, a results view, and your name in the header

The web app's selection and format choices now live on the **server**, so every
browser, tab, and device pointed at the app shows the same ticked books, the
same formats, and the same live progress. The header shows your account
email/name instead of a generic "Signed in", and a new results view makes a
failed download easy to find among hundreds of successes.

### Added
- **Server-side shared UI state.** Which books are selected and your preferred
  ebook/audiobook formats now live on the backend (`GET` / `POST /prefs`, and
  folded into the `/activity` poll every browser already makes) instead of each
  browser's `localStorage`/`sessionStorage`. Open the app in a second browser
  mid-download and it shows the same selection and progress. Persisted to
  `LITRES_STATE_FILE` (defaults onto the Docker `/data` volume), so it also
  survives a restart.
- **Account identity in the header.** A cookie-only session (e.g. in Docker,
  where there's no OS keychain to remember the login name) now recovers your
  email/login from litres.ru's `/users/me`, so the header shows who you are
  rather than a generic "Signed in".
- **Results view with a status filter.** After a build, a summary shows
  `All N · ✓ done · ! skipped · ✗ failed` as clickable pills -- one click
  filters the log to just the failures, so a single rights-limited title no
  longer hides among hundreds of successes.
- **End-to-end / smoke tests.** An offline e2e suite (real server boot + full
  login → build → download flow + the MCP tool flow) plus an opt-in
  `pytest -m live` suite that smoke-tests a running instance.

### Fixed
- **The results and the download link survive a page reload.** The cache-only
  size-check that runs on every page load used to wipe the finished build's
  per-book results *and* its zip download link. Both are now kept until the
  next build starts, so you can reload to inspect failures and still download
  the zip.
- A build where every selected title failed no longer offers an empty zip.

### Changed
- New `LITRES_STATE_FILE` setting (default `.litres_state.json`; `/data/...`
  in Docker) holds the shared UI state; it's git-ignored like the session and
  cache files.

## [0.11.1] - Faster release builds (cached Docker layers)

### Changed
- The Docker publish workflow now uses a **registry-backed build cache** (a
  per-image `buildcache` tag in GHCR) instead of the GitHub Actions cache.
  That cache only runs on tags and is scoped per git ref, so every release
  rebuilt from scratch -- re-pulling the ~2.5GB Playwright base and re-running
  `pip install` for both amd64 and arm64. The registry cache is ref-agnostic,
  so releases reuse unchanged layers and build far faster. CI-only change.

## [0.11.0] - Renamed to BookVault + trademark/non-affiliation notices

Renamed the project from `litres-assistant` to **BookVault** so its identity no
longer leads with a third-party trademark, and added clear non-affiliation
notices. The project only *refers* to litres.ru (the service it backs up your
own purchases from) -- it is not affiliated with LitRes.

### Changed (breaking)
- **Project renamed to `bookvault`.** Repo, Python distributions
  (`bookvault-core` / `-web` / `-mcp`), importable modules (`bookvault_core` /
  `bookvault_web` / `bookvault_mcp`), console commands (`bookvault-web`,
  `bookvault-mcp`), and Docker images (`ghcr.io/mavrovde/bookvault/{web,mcp}`)
  all use the new name. The OS-keychain service and MCP server id are now
  `bookvault`.
- **Kept as-is (nominative references to the litres.ru _service_):** the
  `LITRES_*` environment variables, the `LitresClient` class and its errors,
  litres.ru URLs, and the `.litres_session.json` / `.litres_cache.json` data
  files. These describe what the tool connects to, which trademark law permits.

### Added
- **`TRADEMARKS.md`** and non-affiliation notices in the README, the MCP
  README, and the LICENSE: "LitRes"/"ЛитРес"/"litres.ru" are trademarks of
  ООО «ЛитРес»; BookVault is independent and unofficial, uses the name only
  nominatively, and uses none of LitRes's logos/branding. Links to the
  official LitRes sites are included.
- The LICENSE now spells out that it covers this project's own code only and
  grants no rights in any third-party trademark.

### Migration
- Reinstall the packages: `pip install -e ./core -e ./web -e ./mcp`.
- Use the `bookvault-web` / `bookvault-mcp` commands (was `litres-web` /
  `litres-mcp`). Pull the new images from `ghcr.io/mavrovde/bookvault/*`.
- Your saved session/keychain login carries over only partially (the keychain
  service id changed); if the web app shows logged-out, just log in once more.

## [0.10.1] - Multi-arch Docker images

### Fixed
- **The published Docker images are now multi-arch (`linux/amd64` +
  `linux/arm64`).** 0.10.0 built amd64 only, so `docker pull` / `docker compose
  up` failed on Apple Silicon (arm64) machines with "no matching manifest for
  linux/arm64". The publish workflow now sets up QEMU and builds both.

### Changed
- Bumped the Docker publish workflow's actions to their Node 24 versions
  (`docker/build-push-action@v7`, `login-action@v4`, `metadata-action@v6`,
  `setup-buildx-action@v4`, plus `setup-qemu-action@v4`), clearing the
  "Node.js 20 is deprecated" warnings.

## [0.10.0] - Docker images for the web app and MCP server

Both entry points now ship as container images, published to the GitHub
Container Registry on every release (`ghcr.io/mavrovde/bookvault/web`
and `.../mcp`, tagged with the version + `latest`). A `docker compose up -d`
starts and controls both.

### Added
- **Two Dockerfiles** (`Dockerfile.web`, `Dockerfile.mcp`) on the official
  Playwright image, so Chromium and its system libraries are already present.
- **`docker-compose.yml`** runs both services sharing one named volume
  (`litres-data` at `/data`) -- so logging in through the web app also
  authenticates the MCP server (shared session cookies), and the cache +
  downloads persist across restarts. Ports are published to `127.0.0.1` only,
  preserving the localhost-only design.
- **`.github/workflows/docker-publish.yml`** builds and pushes both images to
  ghcr.io on each `v*` tag.
- **MCP `streamable-http` transport.** In a container there's no stdin to
  attach, so the MCP server can run as a long-lived networked service
  (`LITRES_MCP_TRANSPORT=streamable-http`, reachable at
  `http://host:8421/mcp`) that Compose can start/stop. Defaults to stdio for
  direct/Claude-Desktop use.
- New env vars: `LITRES_APP_HOST`, `LITRES_RELOAD`, `LITRES_MCP_TRANSPORT`,
  `LITRES_MCP_HOST`, `LITRES_MCP_PORT`.

### Changed
- **`credentials` degrades gracefully when there's no OS keychain** (a headless
  container): `save`/`load_last`/`forget` catch `NoKeyringError` and fall back
  to session-only instead of crashing the login. No password is written to the
  container -- the saved browser session (on the volume) keeps you logged in;
  re-login via the web form once it lapses.
- The web server's bind host and reload are now env-configurable
  (`LITRES_APP_HOST`, `LITRES_RELOAD`) -- defaults unchanged for local runs.
- **README overhaul** -- restructured with a table of contents, badges,
  features, and clearer quick-start paths, plus a **Legal & fair use** section
  (this tool only backs up books you have fairly purchased -- no DRM
  circumvention, no redistribution).
- Added a **LICENSE** (MIT, with an attribution requirement: credit the author
  and link the source in any distribution or derivative work).

## [0.9.0] - Don't provoke DDoS-Guard

DDoS-Guard (litres.ru's anti-bot layer) decides "bot or human" from the TLS
handshake (JA3/JA4), the HTTP request shape, request cadence, and IP -- not
just cookies. This release makes the client behave like the low-volume,
personal tool it is, so it stops tripping those false-positive checks.

### Fixed
- **Downloads now carry the browser's TLS fingerprint.** API calls run inside
  Chromium, but downloads stream over a separate HTTP client (Playwright's
  request client can't stream, and audiobook bundles reach ~2GB). That client
  was plain `httpx` -- a Python/OpenSSL JA3/JA4 that, even with valid `__ddg*`
  cookies, could be re-challenged/403'd. `download_file` now uses `curl_cffi`
  impersonating Chrome, so the download's fingerprint matches the session that
  solved the challenge (measured: JA3 and JA4 both differ from httpx, and it
  negotiates HTTP/2 like Chrome). Falls back to `httpx` if `curl_cffi` isn't
  importable.

### Added
- **Retry with backoff + cookie re-warm on transient blocks.** A DDoS-Guard
  403 / 429 / 503 now triggers: honor `Retry-After`, jittered exponential
  backoff, a `__ddg*` cookie re-warm via a quick page visit, then retry --
  instead of failing the item and immediately hitting the next one (the
  pattern that hardens a soft block). A genuine litres 403 (rights-limited
  book) carries no DDoS-Guard signature and is not retried. New env vars:
  `LITRES_MAX_RETRIES`, `LITRES_RETRY_BASE_DELAY`, `LITRES_RETRY_MAX_DELAY`.
- **Instrumentation:** failed downloads/API calls log the response `Server`
  header, so a DDoS-Guard block is distinguishable from a litres app error in
  the logs.

### Changed
- **Opening the app no longer sweeps every book's size.** The automatic
  on-load sweep is now cache-only (resolves sizes already on disk, zero
  litres.ru calls); live per-book size fetches happen only on an explicit
  Refresh. This removes the biggest bulk-request pattern the app produced.
- **Jittered pacing** between live size fetches and library pages, so the
  cadence doesn't look mechanically scripted.
- Anti-bot backoff sleeps are interruptible by Stop, so cancelling stays
  responsive even while a request is being retried.

## [0.8.0] - Live download progress; library survives long downloads

### Added
- Live per-file download progress. `download_file` now takes an
  `on_progress(written, total)` callback, invoked after each streamed 1 MiB
  chunk with the bytes written so far and the total from `Content-Length`
  (or `None` if the server sends none). The zip-build activity threads this
  into its state machine (`current_downloaded` / `current_total`), so the UI
  can show a live `12.3 / 45.0 MB` readout for the file currently
  downloading.

### Changed
- The zip-build progress bar now reflects **bytes**, not just whole books.
  It blends the current file's download fraction into the book count
  (`(books_done + current_file_fraction) / total`), so a single-book job (or
  the last book of any job) visibly fills mid-transfer instead of sitting at
  0% until that one file finishes. Whole-library jobs (book count unknown)
  fill by the current file's byte fraction so the bar still moves.

### Fixed
- The library no longer vanishes during a long download. A cache-miss
  `/library` fetch runs on the single Playwright worker thread; while that
  thread is busy with an activity (e.g. a large audiobook download) the
  request used to block for the whole download, and a page reload showed
  "Could not load your library." The route now serves the slightly-stale
  cached list when an activity is in progress, and the frontend keeps any
  list it already has (retrying quietly) rather than blanking it on a
  transient failure.

## [0.7.3] - Cancel interrupts an in-progress download

### Changed
- Stop now interrupts the file that's currently downloading, not just the
  queue between books. `download_file` polls a cancel callback between
  streamed chunks, so a Stop takes effect within a fraction of a second
  (measured ~60 ms mid-transfer) and the partial file is discarded --
  previously an in-flight ~2GB audiobook had to finish (or hit its timeout)
  before the Stop registered. This builds directly on 0.7.2's chunked
  streaming. A transfer that stalls without sending any bytes still relies on
  the per-file timeout as a backstop.

## [0.7.2] - Stream downloads to disk; smart per-member zip packing

### Fixed
- Downloads no longer buffer the whole file in memory. `download_file` now
  streams the response to disk in 1 MiB chunks (over httpx, reusing the
  browser session's cookies -- incl. the DataDome anti-bot cookies -- and
  captured app-level headers, so the authentication/anti-bot profile is
  unchanged). A ~2 GB audiobook used to mean ~2 GB resident and the machine
  swapping ("system very slow during download"); peak memory is now roughly
  constant regardless of file size.

### Changed
- The download zip is packed per member so macOS Archive Utility opens it
  without re-compressing gigabytes of already-compressed audio (which the
  0.7.1 "DEFLATE everything" fix did, at ~0% size gain and ~2 min CPU per
  audiobook):
  - Audiobook bundles (`zip_with_mp3`) are unpacked and their tracks added
    STORED under a per-book folder -- fast, and no nested-zip
    end-of-central-directory to confuse the parser.
  - Members that are themselves zips (epub, fb2.zip, fb3, ...) are DEFLATEd
    as single files to mask their nested signatures (they're small, so
    cheap).
  - Everything else (m4b, mp3, pdf, txt, mobi) is STORED -- safe and free.

## [0.7.1] - Fix "unsupported format" when opening the downloaded zip

### Fixed
- The library zip is now built with DEFLATE compression instead of STORED.
  Its members (epubs, `zip_with_mp3` audiobooks) are themselves zip files;
  stored uncompressed, each member's raw end-of-central-directory signature
  (`PK\x05\x06`) leaked verbatim into the outer archive, so macOS Archive
  Utility saw several such markers and refused the whole file with
  "Unsupported format" (command-line `unzip`/`ditto`, which read the real
  central directory, were unaffected). A light DEFLATE (level 1) rewrites the
  member bytes so those nested signatures no longer appear raw, at negligible
  CPU/size cost on the already-compressed content. Double-clicking the
  download now extracts correctly, with proper non-Latin (e.g. Cyrillic)
  filenames.

## [0.7.0] - Split into subprojects; env credentials are MCP-only

### Changed
- Restructured the single `app/` package into three subprojects, each with
  its own `pyproject.toml` and runtime dependencies:
  - `core/` (`bookvault-core`) -- the shared library: `client.py`,
    `session.py`, `credentials.py`, `cache.py`.
  - `web/` (`bookvault-web`) -- the web app: `app.py` (was `web.py`),
    `activity.py`, `run.py`, `templates/`, `static/`. Entry point:
    `bookvault-web`.
  - `mcp/` (`bookvault-mcp`) -- the MCP server: `server.py` (was
    `mcp_server.py`) + its own `README.md`. Entry point: `bookvault-mcp`.
  Installing the web app no longer pulls in the MCP SDK, and installing the
  MCP server no longer pulls in FastAPI/uvicorn; both depend on
  `bookvault-core`. Dev tooling and the pytest/ruff config live in a workspace
  root `pyproject.toml` (the flat `requirements*.txt` and `pytest.ini` are
  gone).
- The web app no longer auto-logs-in from `.env` credentials. It restores a
  saved session or re-logs-in from the OS keychain, and otherwise shows its
  login form for interactive login (which persists the session + keychain
  for reuse). `LITRES_LOGIN`/`LITRES_PASSWORD` are now consumed only by the
  headless MCP server (`session.restore_session(allow_env_login=...)`).
- Renamed the CI workflow to `.github/workflows/lint-test-audit.yml` and
  updated it for the new packaging (editable installs of the subprojects,
  environment-scoped `pip-audit`).
- Session/cache files (`.litres_session.json`, `.litres_cache.json`) now
  default to the current working directory (repo root when launched via the
  `bookvault-web`/`bookvault-mcp` commands from there); still overridable via
  `LITRES_SESSION_FILE`/`LITRES_CACHE_FILE`.

## [0.6.0] - Move the activity state machine to the backend

### Changed
- The activity state machine now lives entirely in the backend
  (`app/activity.py`), not the browser. States are `idle -> refreshing /
  checking / preparing / stopping -> idle` with a terminal `result`
  (`done` / `cancelled` / `error`). Only one activity runs at a time,
  matching the single dedicated Playwright worker thread.
- The paced per-book size-check sweep moved server-side: it's now the
  `checking` activity rather than a loop in `app.js`. Pacing and
  selected-books-first ordering happen on the backend; the browser just
  passes its current selection when a sweep starts.
- Library refresh became the `refreshing` activity, which reloads the list
  and then rolls straight into a size sweep -- previously two separate
  frontend steps.
- The browser is now a thin renderer: it POSTs actions
  (`/activity/refresh`, `/activity/check`, `/activity/prepare`,
  `/activity/cancel`) and polls `GET /activity`, painting whatever state it
  reports. Every button's enabled/label state is a pure function of that
  reported state; the frontend holds no activity logic of its own.
- Renamed the download routes to activity routes and merged the download
  job into the state machine: `app/download_job.py` is now `app/activity.py`
  (the zip build is the `preparing` activity). `GET /download/file` (serving
  the finished zip) is unchanged.

## [0.5.0] - Toolbar regrouping, a stoppable size-check, and clearer labeling

### Added
- The activity state machine gained an explicit `STOPPING` state that can
  interrupt either a size-check sweep or a download, not just downloads --
  Stop now works during both.
- Stop is now always visible next to Refresh/Prepare zip (like they are),
  just enabled/disabled by activity, instead of being hidden entirely --
  hiding it meant it could disappear before anyone reacted once a warm
  cache made checking sizes resolve in well under a second.

### Changed
- Regrouped the library toolbar: Refresh/Prepare zip/Stop together as
  library-level actions; search, type filter, and sort together as one
  row since they're all "narrow down the list" controls; selection
  status/actions in their own row.
- Renamed "Download" to "Prepare zip" throughout (button, badge, progress
  text, error alert) -- that action fetches books and builds a zip
  server-side; the actual file download only happens afterward via the
  separate "Save zip file" link, and the old name conflated the two.
- Stop now shares the same button style as Refresh/Prepare zip instead of
  a separate danger-tinted look, per request that the button group look
  consistent.

## [0.4.0] - Caching, a unified progress/activity state machine, and reliability fixes

### Added
- Disk-backed cache (`app/cache.py`) for the library listing (15 min TTL)
  and per-book file listings (7-day TTL) -- repeat page loads, app
  restarts, and starting a download right after browsing the library no
  longer re-query litres.ru for data already fetched moments ago. Cleared
  on login/logout so one account's data can't leak into another's session.
- A "Refresh" button for the library list, next to Download, for the rare
  case you bought something new before the cache would naturally expire.
- An explicit frontend activity state machine (idle/checking/downloading)
  driving one shared, unified progress card instead of two separate
  indicators -- checking sizes and downloading now visibly disable each
  other's controls instead of being able to run concurrently.
- Immediate "Stopping…" feedback when Stop is clicked, since cancellation
  can only take effect between books (documented, unchanged limitation) --
  previously the click looked completely unacknowledged until the current
  book's transfer finished.

### Changed
- The background size-check sweep now prioritizes whichever book you just
  selected instead of waiting for its turn in the full-library queue --
  pacing requests to avoid looking like scraping had made selected books
  wait through however many hundreds of others came first in the list.
- That same sweep only paces itself on genuine live litres.ru calls now
  (the backend reports cache hits explicitly) -- cached books resolve
  instantly regardless of queue position.
- Moved the Download button next to Refresh at the top instead of a
  sticky bottom bar, with matching styling.
- `run.py` now scopes its dev-reload file watcher to `app/` only --
  previously editing a test file mid-session silently restarted the whole
  live server, wiping any in-progress download.

## [0.3.0] - Classic-library redesign, relocated settings, and a mascot

### Added
- Recolored the whole UI into a warm "classic library" theme -- parchment,
  oxblood leather, brass/gold, dark wood -- with a serif heading font
  (Playfair Display), replacing the previous cool modern-SaaS palette.
  Deliberately generic, not tied to any specific copyrighted work.
- A small inline-SVG mascot ("Lito") on the login screen, plus a matching
  brand icon in the top bar and a real favicon -- no build step or binary
  assets needed.
- A generic person icon next to the account name, since the login value
  may be a plain username rather than an email per litres.ru's own docs.
- All CSS/JS extracted out of `index.html` into real files
  (`app/static/css/style.css`, `app/static/js/app.js`), served via a
  mounted `/static` directory, instead of one large inline template.

### Changed
- Preferred e-book/audiobook format pickers moved into the top bar next to
  the account chip, freeing up the vertical space a dedicated "Preferred
  formats" card used to take above the library -- the book grid now uses
  that space instead (up to 78vh vs. 64vh before).

### Fixed
- The account name briefly displayed as the literal string "None" for a
  session restored from saved cookies with no matching keyring entry --
  the cookie-restore path now falls back to `LITRES_LOGIN` like the
  fresh-login path already did.

## [0.2.0] - Bookshelf UI, sorting/filtering, and clearer diagnostics

### Added
- Library browser redesigned as a "bookshelf" grid of book covers (in place
  of a narrow single-column list), showing far more titles on screen at
  once and using the freed-up width of a wider page layout.
- Sort library by title, author, or size, and filter by e-book vs.
  audiobook, alongside the existing title/author search.
- Structured logging (Python `logging`) throughout the login flow, library
  listing, and download job -- login attempts, per-page library fetches,
  per-book download start/success/failure/skip, and job completion are now
  all logged with context (art id, title, timing), configurable via
  `LITRES_LOG_LEVEL`.

### Changed
- Per-book download errors and skips in the UI's progress log now show a
  short, actionable reason (e.g. "Blocked by litres.ru's anti-bot check
  (DDoS-Guard) -- wait a bit, then retry this book.") instead of a generic
  "failed"/"no file"; the raw underlying error is still logged in full and
  available as a tooltip.
- Clarified the "Preferred formats" helper text to explicitly say a book
  missing your chosen format is downloaded in the next-best format instead
  of being skipped.

## [0.1.0] - Initial release

### Added
- Local web app to browse and back up your own purchased litres.ru library:
  login, per-book selection with search/filter, cover thumbnails, authors,
  audio/book tags, and per-book file sizes with a running estimate of the
  selected total.
- Default format pickers for e-books (epub, fb2, mobi, PDF, txt, rtf, ...)
  and audiobooks (zip-of-mp3 vs. single m4b), applied per book with
  automatic fallback when unavailable.
- Live download progress (status badge, progress bar, per-book log) backed
  by a background job the browser polls, plus a Stop button.
- MCP server (`app/mcp_server.py`) exposing `login_status`,
  `login_to_litres`, `list_library`, and `download_book` as tools.
- Playwright-driven login flow to get past litres.ru's DataDome-style bot
  protection, capturing the app-level API headers the site's own frontend
  attaches to every call and reusing them for subsequent requests.
- A single dedicated worker thread (`session.py`) that every
  Playwright-touching call is routed through, since Playwright's sync API
  only tolerates being used from the thread that created it.
- Configuration via environment variables: `LITRES_APP_PORT`,
  `LITRES_DOWNLOAD_DIR`, `LITRES_SESSION_FILE`,
  `LITRES_DOWNLOAD_TIMEOUT_MS`, `LITRES_HEADLESS` (see README).
- Test suite (pytest) covering format-picking logic, pagination/error
  handling, session restore/login/logout precedence, download-job
  orchestration and cancellation, web routes, and MCP tools -- fully
  mocked, no real Playwright/network involved.
- GitHub Actions workflow running the test suite on every push/PR.
- A GitHub MCP server config (`.mcp.json`) for repo contributors, separate
  from the litres MCP server, authenticated via `gh auth token` at
  connection time rather than a stored secret.

### Fixed
- An explicitly empty book selection was silently treated the same as "no
  filter" and downloaded the entire library instead of nothing.
- A stalled download (observed on a real ~350MB audiobook bundle) could
  hang the whole job for up to 30 minutes; the per-file timeout is now 5
  minutes, and one book failing no longer aborts the rest of the run.
- A thread-affinity bug where the download job's own thread (and the MCP
  server's tool implementations) could run Playwright calls on a different
  thread than the one holding the browser session, crashing outright.
