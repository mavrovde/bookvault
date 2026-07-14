<h1 align="center">📚 BookVault</h1>

<p align="center">
  <em>Own the books you bought.</em><br>
  <strong>Back up your own purchased litres.ru library — books &amp; audiobooks — entirely from your own machine.</strong><br>
  Browse what you own, pick the titles and format you want, and download them as a zip — with live progress and a stop button.
</p>

<p align="center">
  <a href="https://github.com/mavrovde/bookvault/actions/workflows/lint-test-audit.yml"><img alt="CI" src="https://github.com/mavrovde/bookvault/actions/workflows/lint-test-audit.yml/badge.svg"></a>
  <a href="https://github.com/mavrovde/bookvault/actions/workflows/docker-publish.yml"><img alt="Docker images" src="https://github.com/mavrovde/bookvault/actions/workflows/docker-publish.yml/badge.svg"></a>
  <a href="https://www.python.org/"><img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white"></a>
  <a href="https://github.com/mavrovde/bookvault/releases"><img alt="Latest release" src="https://img.shields.io/github/v/release/mavrovde/bookvault?sort=semver"></a>
  <a href="#-security--privacy"><img alt="Runs 100% local" src="https://img.shields.io/badge/runs-100%25%20local-brightgreen"></a>
  <a href="LICENSE"><img alt="License: MIT with attribution" src="https://img.shields.io/badge/license-MIT%20(attribution)-yellow.svg"></a>
</p>

> [!IMPORTANT]
> **Your own books only — no rights are broken.** This tool logs in with **your own** litres.ru account and
> can download **only the titles you have legally purchased** — nothing else is reachable. It makes personal
> backup copies of content you already own (format-shifting), using your own authenticated session and the
> site's own download endpoints. It does **not** crack DRM, bypass payment, or grant access to anything you
> haven't bought. Keep your downloads for personal use and don't redistribute them. See [Legal &amp; fair use](#-legal--fair-use).

> [!NOTE]
> **Not affiliated with LitRes.** BookVault is an independent, unofficial project and is not affiliated with,
> endorsed by, or sponsored by ООО «ЛитРес» (LLC "LitRes"). "LitRes", "ЛитРес", and "litres.ru" are trademarks
> of their owner, used here only to describe compatibility. See [Trademarks](#trademarks) and [`TRADEMARKS.md`](TRADEMARKS.md).

It comes in **three flavours** that share the same backend and login/session code:

- 🖼️ **A native desktop app** (macOS / Windows / Linux) — the web app in a real window, no browser or terminal. [Download for macOS](https://github.com/mavrovde/bookvault/releases/latest).
- 🖥️ **A local web app** — browse your library, tick the books you want, choose a format, and download.
- 🔌 **An MCP server** — so Claude (or any MCP client) can list your library and download titles as tools.

---

## Contents

- [Features](#-features)
- [Quick start](#-quick-start)
- [Using the web app](#-using-the-web-app)
- [Desktop app (macOS / Windows / Linux)](#-desktop-app-macos--windows--linux)
- [Using it from Claude (MCP)](#-using-it-from-claude-mcp)
- [Running in Docker](#-running-in-docker)
- [Configuration](#-configuration)
- [How it works](#-how-it-works)
- [Development &amp; tests](#-development--tests)
- [Legal &amp; fair use](#-legal--fair-use)
- [Security &amp; privacy](#-security--privacy)
- [Known limitations](#-known-limitations)
- [License](#-license)

---

## ✨ Features

- **📖 Books &amp; 🎧 audiobooks** — pick exactly which titles to back up, with cover thumbnails, authors, and file sizes.
- **🎯 Format of your choice** — set a preferred ebook format (epub, fb2, pdf, …) and audiobook format, with sensible fallbacks per title.
- **📦 One tidy zip** — ebooks as single files, each audiobook as a folder of its tracks; packed so macOS Archive Utility opens it cleanly.
- **⏳ Live progress + Stop** — a byte-level progress bar (`12.3 / 45.0 MB`) and a Stop button that interrupts even a mid-transfer download.
- **✅ Results at a glance** — when a build finishes, a summary shows `✓ done · ! skipped · ✗ failed` as clickable filters, so a single rights-limited title never hides among hundreds of successes. The results and the download link survive a page reload.
- **🔄 Same view in every browser** — your selection, format choices, and live progress live on the server (not per-browser), so a second browser or tab — or another device on your machine — shows exactly the same thing.
- **🛡️ Anti-bot resilient** — matches the browser's TLS fingerprint on downloads and retries transient DDoS-Guard blocks automatically (details [below](#-how-it-works)).
- **⚡ Smart caching** — your library and file listings are cached on disk, so reloads and restarts stay fast and gentle on litres.ru.
- **🔒 Local &amp; private** — your password lives in your OS keychain (or nowhere, in Docker); nothing is sent anywhere but litres.ru.
- **🖼️ Native desktop app** — the same app in a real window on macOS, Windows, and Linux (no browser tab, no terminal); it starts and stops its own backend. See [Desktop app](#-desktop-app-macos--windows--linux).
- **🐳 Docker-ready** — two published images and a one-command `docker compose up`.

---

## 🚀 Quick start

Pick whichever fits you.

### Option A — Download the macOS app (no terminal, no Python)

Grab **[`BookVault.dmg` from the latest release](https://github.com/mavrovde/bookvault/releases/latest)**,
open it, and drag **BookVault** to Applications. First launch: right-click the
app → **Open** (it's an unsigned build, so Gatekeeper asks once), then log in.
On first run it downloads the browser engine it needs (~150 MB, one time) — a
splash shows the progress. See [Desktop app](#-desktop-app-macos--windows--linux).

### Option B — Docker (easiest cross-platform, nothing to install but Docker)

```bash
git clone https://github.com/mavrovde/bookvault.git
cd bookvault
docker compose up -d
```

Open **http://127.0.0.1:8420**, log in with your litres.ru account, and you're set.
Full details in [Running in Docker](#-running-in-docker).

### Option C — Run it locally (Python 3.11+)

```bash
git clone https://github.com/mavrovde/bookvault.git
cd bookvault
python3 -m venv .venv
.venv/bin/pip install -e ./core -e ./web        # the web app + its shared core
.venv/bin/playwright install chromium           # one-time browser download
.venv/bin/bookvault-web
```

Then open **http://127.0.0.1:8420** and log in. Your password is remembered in your OS keychain, so you won't have to log in again next time.

> 💡 Don't have Python? Grab it from [python.org](https://www.python.org/downloads/). On macOS/Linux it's often already installed.

---

## 🖥️ Using the web app

1. **Log in** once with your litres.ru email and password.
2. **Browse &amp; filter** your library — search by title/author, filter books vs. audiobooks, sort by title/author/size.
3. **Select** the titles you want (nothing is pre-selected, so you never start a huge download by accident).
4. **Pick a format** (optional) — your preferred ebook and audiobook formats, used when available.
5. **Prepare zip** — watch the live progress bar; hit **Stop** anytime.
6. **Review results** — the summary tallies `✓ done · ! skipped · ✗ failed`; click a pill to filter to just those (e.g. the one rights-limited title that couldn't be downloaded).
7. **Download** the zip when it's ready.

> Your selection, format choices, and progress are kept **on the server**, so opening the app in another browser/tab shows the same view — and the results and download link stick around after a reload.

> **Opening the zip:** double-click it (Finder / Archive Utility) or any modern tool.
> ⚠️ macOS's built-in Terminal `unzip` garbles Cyrillic filenames — extract via Finder, or run
> `ditto -x -k litres-library.zip dest/` for correct names.

---

## 🖼️ Desktop app (macOS / Windows / Linux)

Prefer a real app window to a browser tab? `bookvault-desktop` runs the **same
web app inside a native OS window** (WKWebView on macOS, WebView2 on Windows,
WebKitGTK on Linux) via [pywebview](https://pywebview.flowrl.com/) — no browser,
no terminal to keep open. It **starts the backend for you**: the app launches the
web server on a private `127.0.0.1` port in the background, shows a brief splash
while your saved session restores, then loads the app — and closing the window
shuts the backend down cleanly. It reuses `bookvault-web` verbatim (the backend
is *imported*, not duplicated), so it's the same library browser, formats,
progress, and results as the web app.

### Install (macOS)

Download **[`BookVault.dmg` from the latest release](https://github.com/mavrovde/bookvault/releases/latest)**,
open it, and drag **BookVault** to Applications.

- **First launch:** it's an *unsigned* build, so macOS Gatekeeper blocks a plain
  double-click. Right-click the app → **Open** → **Open** (once), or clear the
  quarantine flag: `xattr -dr com.apple.quarantine /Applications/BookVault.app`.
- **First run downloads the browser engine** it needs for the litres.ru login
  (~150 MB, one time) — a splash shows progress. After that, launches are quick
  and offline until you log in.
- Your session, cache, and settings live in
  `~/Library/Application Support/BookVault/`; downloads go to
  `~/Downloads/litres-library/`.

### Install (Windows)

Download **[`BookVault-Setup-<version>.exe` from the latest release](https://github.com/mavrovde/bookvault/releases/latest)** and run it. It installs BookVault to `Program Files` with a Start-menu entry (and an optional desktop shortcut).

- **First launch of the installer:** it's an *unsigned* build, so Windows SmartScreen shows a blue "Windows protected your PC" dialog — click **More info → Run anyway**. (Requires the WebView2 runtime, which ships with Windows 11 and up-to-date Windows 10.)
- **First run of the app:** BookVault downloads Chromium (~150 MB) once — the splash shows progress — cached under `%LOCALAPPDATA%\ms-playwright` (survives reinstalls). App data lives in `%LOCALAPPDATA%\BookVault`.

### Install (Linux)

Download **[`BookVault-<version>-x86_64.AppImage` from the latest release](https://github.com/mavrovde/bookvault/releases/latest)**, make it executable, and run it:

```bash
chmod +x BookVault-*-x86_64.AppImage
./BookVault-*-x86_64.AppImage
```

- **Runtime prerequisite — WebKitGTK 4.1.** The window renders with your system's WebKitGTK, which is **not bundled** (WebKit's multiprocess helpers use compile-time absolute paths that can't live inside an AppImage). On Ubuntu 24.04+ / Debian, one line covers it:
  ```bash
  sudo apt-get install -y libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1 gir1.2-gtk-3.0
  ```
  (Distros that still ship only the WebKit2GTK **4.0** ABI aren't supported — this build targets 4.1.)
- **FUSE.** If the AppImage won't mount, `sudo apt-get install -y libfuse2t64` (or `libfuse2` on older releases), or run it with `--appimage-extract-and-run`.
- **First run** downloads Chromium (~150 MB) once (splash shows progress); on a minimal install it also needs a few libs: `sudo apt-get install -y libnss3 libatk-bridge2.0-0 libxkbcommon0 libgbm1 libasound2`.
- App data lives in `~/.local/share/BookVault/`; downloads go to `~/Downloads/litres-library/`.

> Homebrew / winget / AUR channels are still on the way.

### Run from source (any OS)

```bash
.venv/bin/pip install -e ./core -e ./web -e ./desktop
.venv/bin/playwright install chromium        # if you haven't already
.venv/bin/bookvault-desktop
```

### Build the macOS app yourself

[PyInstaller](https://pyinstaller.org/) bundles it into a `.app` + `.dmg`
(Chromium is fetched on first run, so the installer stays ~80 MB):

```bash
.venv/bin/pip install pyinstaller
packaging/macos/build.sh                     # -> packaging/macos/dist/BookVault-<version>.dmg
```

CI (`.github/workflows/desktop-macos.yml`) runs this on every release tag and
attaches the `.dmg` to the GitHub Release.

---

## 🔌 Using it from Claude (MCP)

The MCP server exposes your library to any MCP client (e.g. Claude Desktop) as tools:

| Tool | What it does |
|---|---|
| `login_status()` | Whether there's an active session |
| `login_to_litres(login, password)` | Log in and persist the session |
| `list_library(limit)` | List your purchased titles |
| `download_book(art_id)` | Download one title to `LITRES_DOWNLOAD_DIR` |

**Install &amp; run (stdio):**

```bash
.venv/bin/pip install -e ./core -e ./mcp
.venv/bin/playwright install chromium
.venv/bin/bookvault-mcp        # or: python -m bookvault_mcp.server
```

It speaks the MCP **stdio** protocol, so you point a client at it rather than running it by hand. Claude Desktop config:

```json
{
  "mcpServers": {
    "bookvault": {
      "command": "/path/to/bookvault/.venv/bin/bookvault-mcp",
      "cwd": "/path/to/bookvault"
    }
  }
}
```

Prefer to run it in a container (over HTTP)? See [Pointing an MCP client at the container](#pointing-an-mcp-client-at-the-container). More detail lives in [`mcp/README.md`](mcp/README.md).

---

## 🐳 Running in Docker

Two images are published to the GitHub Container Registry on **every release** — one for the web app, one for the MCP server — both built on the official Playwright image (Chromium included):

| Image | Purpose |
|---|---|
| `ghcr.io/mavrovde/bookvault/web` | The web app |
| `ghcr.io/mavrovde/bookvault/mcp` | The MCP server |

Each release tag (`v0.10.0`, …) publishes images tagged with that version, plus `latest`.

### Compose — run &amp; control both

```bash
docker compose up -d            # start web + mcp (pulls the ghcr images)
# or build locally:  docker compose up -d --build
```

<sub>(Older Docker installs use the hyphenated <code>docker-compose</code> command — same thing.)</sub>

Open **http://127.0.0.1:8420** and log in. Everyday controls:

```bash
docker compose logs -f web      # follow the web app's logs
docker compose stop mcp         # stop just the MCP server
docker compose down             # stop both (the named volume persists)
```

Both services share one named volume (`litres-data` at `/data`), so **logging in through the web app also authenticates the MCP server** — they read the same saved session. Your library cache and downloads persist there across restarts.

### 🔒 Localhost-only, on purpose

The containers bind `0.0.0.0` internally, but Compose publishes their ports to **`127.0.0.1` only** — so nothing beyond your own machine can reach them. This preserves the app's single-user, localhost-only design. **Don't change those port bindings to expose it.**

### 🔑 Credentials in a container

There's no OS keychain in a headless container, so the password is **not stored** — `keyring` gracefully degrades to session-only. The saved browser session (cookies, on the `/data` volume) keeps you logged in across restarts for weeks; when it finally lapses you just log in again through the web form. Nothing sensitive is written to the image or the volume.

<details>
<summary>Prefer the MCP server to bootstrap headlessly from credentials</summary>

Set `LITRES_LOGIN` / `LITRES_PASSWORD` (e.g. in a local `.env` that Compose reads). The shared-session route above needs no stored password at all, so this is optional.
</details>

### Pointing an MCP client at the container

The containerized MCP server speaks **streamable-http** (not stdio), so point a client at its URL:

```json
{ "mcpServers": { "bookvault": { "url": "http://127.0.0.1:8421/mcp" } } }
```

<sub>For a non-Docker setup the server still defaults to stdio (see <a href="#-using-it-from-claude-mcp">above</a>). <code>docker run -i …/mcp</code> with <code>LITRES_MCP_TRANSPORT=stdio</code> also works.</sub>

---

## ⚙️ Configuration

Copy `.env.example` to `.env` to override any defaults. **All of it is optional** — the app works out of the box.

```bash
cp .env.example .env
```

Credentials in `.env` are used by the **MCP server only** (it's headless and bootstraps a first session from them). The **web app never reads them** — you log in through its page, and the session is saved and reused.

<details>
<summary><strong>All environment variables</strong> (click to expand)</summary>

| Variable | Default | Purpose |
|---|---|---|
| `LITRES_LOGIN` / `LITRES_PASSWORD` | — | Credentials to bootstrap a session (**MCP server only**) |
| `LITRES_APP_PORT` | `8420` | Web UI port |
| `LITRES_APP_HOST` | `127.0.0.1` | Web bind host. Leave as-is locally; the Docker image sets `0.0.0.0` and publishes to `127.0.0.1` on the host. **Don't set `0.0.0.0` outside a container** |
| `LITRES_RELOAD` | `1` | Auto-reload the web server on code changes (dev). The Docker image sets `0` |
| `LITRES_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` (client-launched) or `streamable-http` (the container's networked service) |
| `LITRES_MCP_HOST` / `LITRES_MCP_PORT` | `127.0.0.1` / `8421` | Bind host/port for the MCP `streamable-http` transport |
| `LITRES_DOWNLOAD_DIR` | `~/Downloads/litres-library` | Where the MCP server's `download_book` saves files |
| `LITRES_SESSION_FILE` | `.litres_session.json` | Where the browser session (cookies) is cached between runs |
| `LITRES_CACHE_FILE` | `.litres_cache.json` | Where the library/file-listing cache is stored |
| `LITRES_STATE_FILE` | `.litres_state.json` | Where the shared UI state (selected books + format prefs) is stored, so every browser sees the same view (**web app only**) |
| `LITRES_LIBRARY_CACHE_TTL` | `900` (15 min) | How long the cached library listing stays fresh |
| `LITRES_FILES_CACHE_TTL` | `604800` (7 days) | How long a book's cached file listing stays fresh |
| `LITRES_DOWNLOAD_TIMEOUT_MS` | `300000` (5 min) | Per-file download timeout (audiobook bundles can be ~2GB) |
| `LITRES_HEADLESS` | `1` | Set `0` to watch the login flow in a real Chromium window (debugging) |
| `LITRES_LOG_LEVEL` | `INFO` | Log verbosity: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LITRES_MAX_RETRIES` | `3` | Retries on a transient anti-bot block (403 / 429 / 503) before giving up |
| `LITRES_RETRY_BASE_DELAY` | `2.0` | First backoff (seconds) on a block, doubled each retry |
| `LITRES_RETRY_MAX_DELAY` | `30.0` | Cap on any single backoff (seconds) |
| `LITRES_SIZE_CHECK_PACE` | `0.2` | Base gap (seconds, jittered up) between live per-book size fetches |

</details>

`.env` is gitignored and never committed — see [Security &amp; privacy](#-security--privacy) for where your credentials actually live.

---

## 🧠 How it works

A quick tour of the design choices that make this reliable. Skip it if you just want to use the app — expand a section if you're curious.

**One state machine, on the backend.** Everything the app can be *doing* — reloading the library, sweeping sizes, building the zip, cancelling — is a single state machine in `activity.py` (`idle → refreshing / checking / preparing / stopping → idle`). Only one activity runs at a time, which falls out naturally from a single dedicated Playwright worker thread. The browser is a thin renderer: it POSTs an action, polls `GET /activity`, and paints whatever state it reports.

**State lives on the server, not the browser.** The current selection and format preferences sit in `prefs.py` (`GET`/`POST /prefs`, and folded into the `/activity` poll), and a finished build's per-book results + zip link are kept on the state machine until the next build. So opening the app in another browser shows the same view, and a page reload never loses your selection, the results, or the download link.

<details>
<summary>🎭 Why Playwright instead of plain HTTP requests</summary>

<br>

litres.ru's login endpoint rejects plain scripted `POST`s with a generic "incorrect credentials" error regardless of the password — the site sets DataDome-style anti-bot cookies (`__ddg9_`, `__ddg1_`) that only a real, JS-executing browser can obtain. So `LitresClient` drives an actual headless Chromium through the real login form.

Being logged in isn't enough either: the API needs several app-level headers (`app-id`, `session-id`, `client-host`, …) that the site's own frontend attaches to every call. Rather than guessing them, the client captures them once from a request the site's own JS fires right after login, and replays them.

Playwright's sync API is tied to whichever thread created it, so `session.py` funnels every call touching a `LitresClient` through one dedicated worker thread.
</details>

<details>
<summary>🛡️ Staying under DDoS-Guard's radar</summary>

<br>

litres.ru sits behind DDoS-Guard, which decides "bot or human" from more than cookies: it fingerprints the **TLS handshake (JA3/JA4)** and the HTTP request shape, and watches request *cadence* and IP. As a low-volume personal tool, the goal is simply to not trip those false-positive checks:

- **Downloads carry the same TLS fingerprint as the browser.** API calls run inside Chromium, but downloads must *stream* to disk (audiobook bundles reach ~2GB) over a separate HTTP client. A plain-Python client's TLS fingerprint can be re-challenged even with valid cookies, so `download_file` uses [`curl_cffi`](https://github.com/lexiforest/curl_cffi) impersonating Chrome — its JA3/JA4 matches the session that solved the challenge. (Falls back to `httpx` if `curl_cffi` isn't available.)
- **Transient blocks are retried, not hammered past.** On a 403 / 429 / 503 the client honours `Retry-After`, backs off with jittered exponential delay, re-warms the `__ddg*` cookies via a quick page visit, and retries — instead of failing and immediately hitting the next request. A genuine rights-limited 403 carries no DDoS-Guard signature and isn't retried; instead the client automatically tries the subscription download endpoint.
- **No bulk sweeps on load.** Opening the app resolves only cached sizes; live per-book fetches happen on an explicit Refresh, with jittered pacing.
- **Keep your IP stable.** `__ddg9_` encodes your public IP; flipping a VPN mid-session can force a re-challenge.
</details>

<details>
<summary>📦 Project layout</summary>

<br>

```text
core/                 bookvault-core — shared library (own pyproject.toml)
  bookvault_core/
    client.py         Playwright-driven login + library/file/download calls
    session.py        login/session-restore + the single dedicated Playwright thread
    credentials.py    password storage via the OS keychain (keyring)
    cache.py          disk cache for library + per-book file listings
web/                  bookvault-web — the web app (depends on bookvault-core)
  bookvault_web/
    app.py            FastAPI: library browser, format defaults, activity control
    activity.py       the one backend state machine
    prefs.py          server-side shared UI state (selection + format prefs)
    run.py            starts uvicorn; the `bookvault-web` command
    templates/ static/  HTML + CSS + JS (no build step, no framework)
mcp/                  bookvault-mcp — the MCP server (depends on bookvault-core)
  bookvault_mcp/server.py  MCP tools; the `bookvault-mcp` command
desktop/              bookvault-desktop — native window (depends on bookvault-web)
  bookvault_desktop/app.py  embeds the web app in a pywebview window; `bookvault-desktop`
packaging/macos/      PyInstaller spec + build.sh → BookVault.app / .dmg
tests/                pytest suite — fully mocked, no real Playwright/network
Dockerfile.web        web-app image (Playwright base)
Dockerfile.mcp        MCP-server image
docker-compose.yml    runs + controls both together
```

Each subproject has its own `pyproject.toml` and dependencies: installing `bookvault-web` doesn't pull in the MCP SDK, and vice-versa. Both depend on `bookvault-core`.
</details>

---

## 🧪 Development &amp; tests

```bash
# editable installs of the core/web/mcp subprojects + dev tooling (pytest, ruff)
# (add -e ./desktop to work on the desktop app -- it also runs its tests)
.venv/bin/pip install -e ./core -e ./web -e ./mcp -e ".[dev]"
.venv/bin/python -m pytest
```

The whole suite runs **offline in under a couple of seconds**: `LitresClient` is either bypassed (pure logic) or replaced with a fake (`tests/fakes.py`) — no real browser or network call happens. This includes an **end-to-end smoke suite** (`tests/test_e2e_smoke.py`) that boots the real server and drives the full login → build → download flow against the fake backend. CI (`.github/workflows/lint-test-audit.yml`) runs ruff, the test matrix (Python 3.11–3.13), and a dependency-vulnerability audit on every push/PR.

There's also an **opt-in live smoke suite** (`tests/test_smoke_live.py`) that hits a *running* instance over HTTP — deselected by default, run it against a started app with:

```bash
.venv/bin/python -m pytest -m live        # defaults to http://127.0.0.1:8420
# or point it elsewhere: BOOKVAULT_BASE_URL=http://127.0.0.1:8420 pytest -m live
```

---

## ⚖️ Legal &amp; fair use

This tool is for making **personal backups of books you have fairly bought** on litres.ru — and nothing more.

- ✅ **Only your own purchases.** It authenticates with *your* litres.ru account and can only reach titles *you* have legally bought. It cannot access, list, or download anyone else's library or any book you haven't purchased.
- ✅ **Personal backup / format-shifting.** It saves copies of content you already own so you can keep and read them on your own devices.
- ✅ **The site's own endpoints, your own session.** It uses litres.ru's normal download endpoints through your logged-in session — the same files the site would give you.
- 🚫 **No DRM circumvention, no piracy.** It does not crack DRM, bypass payment, or unlock anything you haven't bought.
- 🚫 **Don't redistribute.** The books remain the property of their rights holders — keep your downloads private and for personal use only.

You are responsible for using this tool in line with litres.ru's Terms of Service and the copyright law that applies to you. If in doubt, don't. The author provides this software as-is and accepts no liability for misuse (see [License](#-license)).

### Trademarks

**BookVault is an independent, unofficial project — not affiliated with, endorsed by, or sponsored by ООО «ЛитРес» (LLC "LitRes").** "LitRes", "ЛитРес", "litres.ru", and "litres.com" are trademarks or registered trademarks of their owner (ООО «ЛитРес», Moscow). BookVault uses the word "litres" **only nominatively** — to describe what it's compatible with — and uses none of LitRes's logos or branding. Official LitRes sites: [litres.com](https://litres.com/) · [litres.ru](https://www.litres.ru/) · [About LitRes](https://litres.com/about-us/). Full notice: [`TRADEMARKS.md`](TRADEMARKS.md).

---

## 🔒 Security &amp; privacy

- 🔑 Your password is stored in your **OS keychain** (`keyring`), never in a plaintext file — and in Docker it isn't stored at all (session-only).
- 🍪 Your browser **session cookies** are cached in a local JSON file so you don't re-login every run. It's gitignored — **treat it like being logged in; don't share it.**
- 🚫 `.env`, `.venv/`, and the session/cache files are all gitignored.
- 🏠 All three front-ends (web, desktop, MCP) are **single-user and local-only by design** — bound to `127.0.0.1` (or published only to it in Docker; the desktop app serves on a private localhost port inside its own process). There's no multi-user support, and none is planned: this is intentionally *not* built to hold other people's credentials.

Found a security issue? See [`SECURITY.md`](SECURITY.md).

---

## ⚠️ Known limitations

Tracked as [GitHub issues](https://github.com/mavrovde/bookvault/issues):

- **Stop is near-instant** — cancelling interrupts the file currently downloading (polled between streamed chunks; the partial file is discarded). A transfer that stalls without sending any bytes still has to hit its timeout first.
- **Edge cases** — response-shape assumptions for the library/file endpoints were confirmed against a limited sample of real items; unusual libraries (podcasts, webtoons, DRM-restricted items) may need follow-up fixes.
- **Zip filenames on macOS Terminal** — the built-in `unzip` garbles non-Latin (e.g. Cyrillic) names. Extract via Finder or `ditto -x -k litres-library.zip dest/`.

---

## 📄 License

Free and open source under the **[MIT License, with attribution](LICENSE)**. Use it, modify it, and share it freely — the only ask is that any distribution or derivative work **visibly credits the original author (Sergii Mavrov) and links back to this repository**. It comes with no warranty.

This license covers BookVault's own source code only; it grants no rights in any third-party trademark (see [Trademarks](#trademarks) and [`TRADEMARKS.md`](TRADEMARKS.md)).
