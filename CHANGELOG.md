# Changelog

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
