---
name: dev-setup
description: Set up (or repair) the local development environment for BookVault — editable installs, Chromium, and the test tooling. Use on a fresh clone or when imports/console scripts stop working.
---

# Dev setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ./core -e ./web -e ./mcp -e ".[dev]"
.venv/bin/playwright install chromium          # one-time, ~150 MB
```

Add `-e ./desktop` if you're working on the native window (needs `pywebview`).

Verify:

```bash
.venv/bin/python -m pytest          # should be ~seconds, fully offline
.venv/bin/python -m ruff check .
```

## Use `python -m`, not the console scripts

Prefer `.venv/bin/python -m pytest` over `.venv/bin/pytest`, and
`.venv/bin/python -m pip` over `.venv/bin/pip`.

Console scripts bake an **absolute** interpreter path into their shebang. If
the repo is ever moved or renamed, every script in `.venv/bin` breaks with
`bad interpreter: .../no such file or directory` while `.venv/bin/python`
itself keeps working (it resolves through `pyvenv.cfg`). `python -m` sidesteps
the shebang entirely.

## If imports fail with `ModuleNotFoundError: bookvault_core`

The venv predates the litres-assistant → bookvault rename, or was never
populated. Re-run the editable install above; confirm with:

```bash
.venv/bin/python -m pip list | grep -i bookvault
```

You should see `bookvault-core`, `bookvault-mcp`, `bookvault-web`, and
`bookvault-workspace`. Stale `litres-*` entries alongside them are harmless
leftovers, but they can confuse `pip-audit` output.

## Optional environment

Copy `.env.example` to `.env` for local overrides. The **web app never
auto-logs-in from `.env`** — those credentials are read by the headless MCP
server only. Log in through the web UI; the session and keychain entry are
then reused.
