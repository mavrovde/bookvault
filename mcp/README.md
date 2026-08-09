# bookvault-mcp

An MCP server that exposes your purchased litres.ru library to MCP clients
(e.g. Claude Desktop) as tools. It reuses `bookvault-core` for login, session
management, and the Playwright-driven litres.ru client -- same session and
OS-keychain credentials as the web app.

> BookVault is an independent, unofficial project, not affiliated with or
> endorsed by ООО «ЛитРес» (LLC "LitRes"). "LitRes"/"litres.ru" are trademarks
> of their owner, used here only to describe compatibility. See
> [`../TRADEMARKS.md`](../TRADEMARKS.md).

## Tools

- `login_status()` -- whether there's an active, working session.
- `login_to_litres(login, password)` -- log in and persist the session.
- `list_library(limit=50)` -- list purchased books/audiobooks.
- `download_book(art_id)` -- download one title to `LITRES_DOWNLOAD_DIR`.

## Install

From the repo root (the MCP server needs `bookvault-core` and a Chromium
build for Playwright):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ./core -e ./mcp
.venv/bin/playwright install chromium
```

## Run

```bash
.venv/bin/bookvault-mcp        # or: .venv/bin/python -m bookvault_mcp.server
```

By default it speaks the MCP **stdio** protocol, so you normally point a
client at it rather than running it by hand. Claude Desktop config:

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

### Networked transport / Docker

Set `LITRES_MCP_TRANSPORT=streamable-http` to run it as a long-lived HTTP
service instead of stdio (binds `LITRES_MCP_HOST:LITRES_MCP_PORT`, default
`127.0.0.1:8421`). This is how the Docker image runs it -- a container has no
stdin to attach. Clients then connect by URL:

```json
{ "mcpServers": { "bookvault": { "url": "http://127.0.0.1:8421/mcp" } } }
```

See the repo root `README.md` ("Running in Docker") for the `docker compose`
setup that runs this alongside the web app and shares its login session.

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
| `LITRES_MCP_TRANSPORT` | `stdio` | `stdio` (client-launched) or `streamable-http` (networked service; used by Docker). |
| `LITRES_MCP_HOST` / `LITRES_MCP_PORT` | `127.0.0.1` / `8421` | Bind host/port for the `streamable-http` transport. |
| `LITRES_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG`/`INFO`/`WARNING`/`ERROR`). Logs go to stderr -- under stdio, stdout is the MCP transport. |

See the repo root `README.md` for the overall project and the web app.
