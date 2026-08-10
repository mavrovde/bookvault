---
name: run-app
description: Run the BookVault web, MCP, or desktop app locally — including how to drive the logged-in UI in a browser without a real litres.ru account. Use when asked to start, demo, or visually verify the app.
---

# Running the app

```bash
.venv/bin/bookvault-web        # http://127.0.0.1:8420  (LITRES_APP_PORT to change)
.venv/bin/bookvault-mcp        # MCP server over stdio
.venv/bin/bookvault-desktop    # native window (needs -e ./desktop)
docker compose up -d --build   # web + mcp, published to 127.0.0.1 only
```

If the console scripts fail with a bad-interpreter error, see the `dev-setup`
skill — use `.venv/bin/python -m bookvault_web.run` instead.

## Verifying the logged-in UI without an account

Most of the interface only exists when logged in, and logging in for real
launches Chromium and hits litres.ru. To drive the full UI offline, boot the
real app against the test fake — the same `FakeLitresClient` the suite uses:

```python
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")                      # repo root, for tests.fakes

state = Path(tempfile.mkdtemp(prefix="bv-demo-"))
os.environ["LITRES_STATE_FILE"] = str(state / ".litres_state.json")
os.environ["LITRES_SESSION_FILE"] = str(state / ".litres_session.json")
os.environ["LITRES_CACHE_FILE"] = str(state / ".litres_cache.json")

import uvicorn
from bookvault_core import session
from bookvault_web.app import app
from tests.fakes import FakeLitresClient

fake = FakeLitresClient(
    library=[{"id": 1, "title": "Book One", "art_type": 0, "persons": [], "cover_url": None}],
    files_by_id={1: [{"id": 100, "extension": "epub", "is_additional": False, "size": 1_000_000}]},
)
session._state["client"], session._state["login"] = fake, "demo@example.com"
uvicorn.run(app, host="127.0.0.1", port=8499, log_level="warning")
```

Point the environment variables at a temp directory (as above) so the demo
never touches your real session, cache, or saved preferences.

Then drive it with the Playwright MCP browser tools, or poll the API directly:

```bash
curl -s http://127.0.0.1:8499/activity | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8499/activity/prepare-zip -H 'Content-Type: application/json' -d '{}'
```

## Running the MCP server logs you in for real

`bookvault-mcp` calls `load_dotenv()` at import, so launching it from the repo
root picks up the **real credentials in `.env`**, launches Chromium, and hits
litres.ru with the account owner's session. That is the documented design (the
headless server has no login form), but it makes a casual "does it start?"
probe a live request against a real account.

Redirecting the state, session and cache files and nulling the keychain is
**not** enough — none of them is where the credentials come from:

```bash
LITRES_LOGIN= LITRES_PASSWORD= \
LITRES_SESSION_FILE=/tmp/probe/.litres_session.json \
LITRES_CACHE_FILE=/tmp/probe/.litres_cache.json \
  .venv/bin/bookvault-mcp     # still logs in: .env wins
```

Neutralise `.env` too — run from a different working directory, or point
`DOTENV_PATH`-style isolation at an empty file. To exercise the tools without
any of this, call them in-process against `FakeLitresClient` the way
`tests/test_mcp_server.py` does; to check only that the server boots and
registers its tools, a `tools/list` over stdio needs no session at all.

## Cleaning up

Kill the server (`pkill -f <your script>`). Prefer stopping it by PID
(`kill $(lsof -tnP -iTCP:8420 -sTCP:LISTEN)`) and give it a moment: a
`kill -9` skips the graceful shutdown and orphans the Playwright driver and
its Chromium, which then hold the browser cache and the port.

Browser-tool output (page snapshots, screenshots) lands in `.playwright-mcp/`,
which is git-ignored — a snapshot of a logged-in page contains the library it
was taken from, so it must never be committed. Before committing after a
browser session, confirm nothing slipped in:

```bash
git status --short          # anything unexpected?
git ls-files | grep playwright   # must print nothing
```
