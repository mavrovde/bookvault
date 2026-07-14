# litres-assistant

A small, local-only tool to back up **your own purchased litres.ru library**
(books and audiobooks). Browse your library, pick exactly which titles you
want (with cover thumbnails, authors, and file sizes), choose your preferred
format, and download them as a zip -- with live progress and a stop button.
Runs entirely on your own machine -- your login/password never go anywhere
except litres.ru itself.

It comes in two forms that share the same login/session logic:

- **A local web app** -- browse your library, select books, pick a format,
  and download, with live progress.
- **An MCP server** -- so Claude (or any other MCP client) can list your
  library and download individual books as tools.

---

## Quick start (non-technical)

1. Install [Python 3.11+](https://www.python.org/downloads/) if you don't
   have it already.
2. Open a terminal in this folder and run:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/playwright install chromium
   ```
3. Start it:
   ```
   .venv/bin/python run.py
   ```
4. Open **http://127.0.0.1:8420** in your browser, log in with your
   litres.ru account, select the books you want, and click "Download."

Your password is remembered securely in your OS keychain, so you won't need
to log in again next time.

---

## Quick start (developer)

```bash
git clone https://github.com/mavrovde/litres-assistant.git
cd litres-assistant
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # includes test dependencies
.venv/bin/playwright install chromium

cp .env.example .env   # optional -- credentials here are for the MCP server only
.venv/bin/python run.py
```

The app binds to `127.0.0.1` only (see `run.py`) -- it's a personal,
single-user tool, not a multi-user service.

### Project layout

```
app/
  client.py       LitresClient -- Playwright-driven login + library/file/download API calls
  session.py      shared login/session-restore logic + the single dedicated Playwright thread
  credentials.py  password storage via the OS keychain (the `keyring` package)
  activity.py     the one backend state machine: refresh / size-sweep / zip-build / cancel
  cache.py        disk-backed cache for the library listing and per-book file listings
  web.py          FastAPI app: library browser, format defaults, activity control + status
  mcp_server.py   MCP server exposing the same functionality as tools
  templates/      the web app's HTML; static/ its CSS + JS (no build step, no framework)
run.py            starts the web app (uvicorn)
tests/            pytest suite -- fully mocked, no real Playwright/network involved
```

### One state machine, on the backend

Everything the app can be *doing* -- reloading the library list, sweeping
book sizes, building the download zip, or being cancelled -- is one backend
state machine in `activity.py`, with states `idle -> refreshing / checking
/ preparing / stopping -> idle` and a terminal `result` (`done` /
`cancelled` / `error`). Only one activity runs at a time, which falls
naturally out of the single dedicated Playwright thread (see `session.py`):
there's only ever one worker, so there's only ever one thing to be doing.

The browser is a thin renderer. It POSTs an action (`/activity/refresh`,
`/activity/prepare`, `/activity/cancel`), then polls `GET /activity` and
paints whatever state it reports -- every button's enabled/label state is a
pure function of that state. The frontend owns no activity logic, no pacing,
and no size-fetch loop of its own; those all live in `activity.py`.

### Why Playwright instead of plain HTTP requests?

litres.ru's login endpoint (`api.litres.ru/foundation/api/auth/login`)
rejects plain scripted `POST` requests with a generic "incorrect
credentials" error regardless of whether the password is right -- the site
sets DataDome-style anti-bot cookies (`__ddg9_`, `__ddg1_`) that only a
real, JS-executing browser can obtain. So `LitresClient` drives an actual
headless Chromium browser (via Playwright) through the real login form.

Being logged in isn't sufficient either: the API also requires several
app-level headers (`app-id`, `session-id`, `client-host`, `ui-currency`,
...) that the site's own frontend code attaches to every call. Rather than
guessing/hardcoding them, `LitresClient` captures them once from a request
the site's own JS fires automatically right after login, and replays them
on subsequent calls.

Playwright's sync API is also tied to whichever single thread created it,
so `session.py` funnels every call that touches a `LitresClient` (library
listing, file lookups, downloads) through one dedicated worker thread --
see that module's docstring for the details.

### Running the MCP server

```bash
.venv/bin/python -m app.mcp_server
```

It communicates over stdio, so normally you don't run it directly --
instead point an MCP client at it. For example, in Claude Desktop's config:

```json
{
  "mcpServers": {
    "litres-assistant": {
      "command": "/path/to/litres-assistant/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/litres-assistant"
    }
  }
}
```

Available tools: `login_status`, `login_to_litres(login, password)`,
`list_library(limit)`, `download_book(art_id)`. Downloaded books are saved
to the directory configured by `LITRES_DOWNLOAD_DIR` (see **Configuration**).

This repo also ships a separate, unrelated `.mcp.json` at its root: a
**GitHub** MCP server config (using the hosted `api.githubcopilot.com/mcp/`
endpoint, authenticated by shelling out to `gh auth token` at connection
time -- no token is ever stored in the file) so that Claude Code contributors
working on *this repo* can watch CI runs and open pull requests through MCP
tools. It has nothing to do with litres.ru; requires the `gh` CLI installed
and authenticated locally.

---

## Configuration

Copy `.env.example` to `.env` and fill in your litres.ru credentials:

```
LITRES_LOGIN=you@example.com
LITRES_PASSWORD=your-litres-password
```

These credentials are used by the **MCP server only**. The MCP server is
headless (no login form), so it bootstraps a first session from them. The
**web app never uses them** -- you log in through its login page, and the
session is saved (browser cookies + your OS keychain) and reused on every
later run; if the saved session lapses, it silently re-logs-in from the
keychain. So for web-app-only use you can leave `.env` credentials unset.

Everything else is optional too, with sensible defaults:

| Variable | Default | Purpose |
|---|---|---|
| `LITRES_APP_PORT` | `8420` | Web UI port (host is always `127.0.0.1`, not configurable, by design) |
| `LITRES_DOWNLOAD_DIR` | `~/Downloads/litres-library` | Where the MCP server's `download_book` tool saves files |
| `LITRES_SESSION_FILE` | `.litres_session.json` at the project root | Where the browser session (cookies) is cached between runs |
| `LITRES_DOWNLOAD_TIMEOUT_MS` | `300000` (5 min) | Per-file download timeout. Whole-audiobook bundles can be ~2GB |
| `LITRES_HEADLESS` | `1` | Set to `0` to watch the login flow in a real Chromium window (debugging) |
| `LITRES_LOG_LEVEL` | `INFO` | Verbosity of the app's own log lines (login, library listing, download progress) -- one of `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LITRES_CACHE_FILE` | `.litres_cache.json` at the project root | Where the cached library listing/file listings are stored (see **Caching** below) |
| `LITRES_LIBRARY_CACHE_TTL` | `900` (15 min) | How long the cached library listing stays fresh before a reload re-fetches it |
| `LITRES_FILES_CACHE_TTL` | `604800` (7 days) | How long a book's cached file listing (size/formats) stays fresh |

`.env` is gitignored and never committed. See **Security notes** below for
where your credentials/session actually live.

---

## Running the tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The whole suite runs offline in under a second: `LitresClient` is either
bypassed entirely (pure logic like format-picking) or replaced with a fake
that mimics its interface (see `tests/fakes.py`) -- no real Playwright
browser or network call happens during tests. A GitHub Actions workflow
(`.github/workflows/tests.yml`) runs the suite on every push/PR.

---

## Security notes

- Your password is stored in your OS keychain (via the `keyring` package),
  never in a plaintext file.
- Your browser session (cookies) is cached in a local JSON file (see
  `LITRES_SESSION_FILE` above), so you don't have to log in on every run.
  This file is gitignored -- **do not commit or share it**, it's equivalent
  to being logged into your account.
- `.env`, `.venv/`, and the session file are all gitignored.
- Both the web app and the MCP server are single-user and local-only by
  design. There is no multi-user support, and none is planned -- see the
  architecture notes above for why (this is intentionally *not* built to
  hold other people's credentials).

---

## Known limitations

Tracked as [GitHub issues](https://github.com/mavrovde/litres-assistant/issues):

- Whole-audiobook downloads can be ~2GB and are currently buffered in
  memory before being written to disk.
- Cancelling a download takes effect between books, not mid-transfer --
  Python/Playwright can't safely preempt a request that's already in
  flight, so a stuck book still has to hit its own timeout.
- Response-shape assumptions for the library/file-listing endpoints were
  confirmed against a limited sample of real library items; edge cases in
  large/varied libraries (podcasts, webtoons, DRM-restricted items) may need
  follow-up fixes.
