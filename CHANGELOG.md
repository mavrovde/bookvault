# Changelog

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
  - `core/` (`litres-core`) -- the shared library: `client.py`,
    `session.py`, `credentials.py`, `cache.py`.
  - `web/` (`litres-web`) -- the web app: `app.py` (was `web.py`),
    `activity.py`, `run.py`, `templates/`, `static/`. Entry point:
    `litres-web`.
  - `mcp/` (`litres-mcp`) -- the MCP server: `server.py` (was
    `mcp_server.py`) + its own `README.md`. Entry point: `litres-mcp`.
  Installing the web app no longer pulls in the MCP SDK, and installing the
  MCP server no longer pulls in FastAPI/uvicorn; both depend on
  `litres-core`. Dev tooling and the pytest/ruff config live in a workspace
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
  `litres-web`/`litres-mcp` commands from there); still overridable via
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
