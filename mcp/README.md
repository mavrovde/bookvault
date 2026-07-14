# litres-mcp

An MCP server that exposes your purchased litres.ru library to MCP clients
(e.g. Claude Desktop) as tools. It reuses `litres-core` for login, session
management, and the Playwright-driven litres.ru client -- same session and
OS-keychain credentials as the web app.

## Tools

- `login_status()` -- whether there's an active, working session.
- `login_to_litres(login, password)` -- log in and persist the session.
- `list_library(limit=50)` -- list purchased books/audiobooks.
- `download_book(art_id)` -- download one title to `LITRES_DOWNLOAD_DIR`.

## Install

From the repo root (the MCP server needs `litres-core` and a Chromium
build for Playwright):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./core -e ./mcp
.venv/bin/playwright install chromium
```

## Run

```bash
.venv/bin/litres-mcp        # or: .venv/bin/python -m litres_mcp.server
```

It speaks the MCP stdio protocol, so you normally point a client at it
rather than running it by hand. Claude Desktop config:

```json
{
  "mcpServers": {
    "litres-assistant": {
      "command": "/path/to/litres-assistant/.venv/bin/litres-mcp",
      "cwd": "/path/to/litres-assistant"
    }
  }
}
```

## Configuration (environment)

Unlike the web app, the MCP server has no interactive login form, so it
bootstraps a first session from credentials in the environment (a `.env`
file in the working directory is loaded automatically). **These credentials
are used by the MCP server only** -- the web app ignores them and logs in
through its page instead.

| Variable | Default | Purpose |
|---|---|---|
| `LITRES_LOGIN` / `LITRES_PASSWORD` | -- | Credentials to bootstrap a session on first run (MCP-only). Once a session exists, it's reused from `LITRES_SESSION_FILE` + the OS keychain. |
| `LITRES_DOWNLOAD_DIR` | `~/Downloads/litres-library` | Where `download_book` saves files. |
| `LITRES_SESSION_FILE` | `.litres_session.json` (CWD) | Cached browser session (cookies), shared with the web app. |
| `LITRES_HEADLESS` | `1` | Set to `0` to watch the login flow in a real Chromium window. |
| `LITRES_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`). Logs go to stderr -- stdout is the MCP transport. |

See the repo root `README.md` for the overall project and the web app.
