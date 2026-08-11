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

## A venv that predates the rename: rebuild it

Console scripts bake an **absolute** interpreter path into their shebang, so a
venv created before the litres-assistant → bookvault rename has every script in
`.venv/bin` pointing at a directory that no longer exists:

```
.venv/bin/pip: bad interpreter: /Users/.../litres-assistant/.venv/bin/python3.13
```

`.venv/bin/python` keeps working (it resolves through `pyvenv.cfg`), which is
why this hides for a long time — and why it is **dangerous**: a command like
`.venv/bin/pip install ruff==0.16.2` fails, prints its error to stderr, and if
you only read the next line you will happily verify the *old* version and
conclude the upgrade was fine.

Check first — if this prints anything, the venv is stale:

```bash
grep -rl "litres-assistant" .venv/bin/ | head
.venv/bin/python -m pip list --editable      # any path outside this repo?
```

**Rebuild rather than patching shebangs.** Such a venv usually also carries
editable installs pointing at the vanished directory (`litres-core`,
`litres-web`, …), stale recorded versions, and a `pyvenv.cfg` whose `command`
names the old path. Rewriting shebangs fixes none of that:

```bash
mv .venv .venv-old                            # keep it until the new one works
python3 -m venv .venv
.venv/bin/python -m pip install -e ./core -e ./web -e ./mcp -e ".[dev]"
.venv/bin/python -m pytest && .venv/bin/ruff check .
rm -rf .venv-old
```

Cheap to do: Playwright's browsers live in `~/Library/Caches/ms-playwright`
(`~/.cache/ms-playwright` on Linux), **outside** the venv, so a rebuild does
not re-download Chromium — `playwright install chromium` is then a no-op.

`python -m` remains the safer habit generally (`.venv/bin/python -m pip`,
`-m pytest`), since it sidesteps the shebang entirely.

## If imports fail with `ModuleNotFoundError: bookvault_core`

The venv was never populated, or predates the rename. Re-run the editable
install above; confirm with:

```bash
.venv/bin/python -m pip list --editable
```

Expect exactly `bookvault-core`, `bookvault-mcp`, `bookvault-web` and
`bookvault-workspace`, all pointing inside this repo and all reporting the
current version. Anything else — especially `litres-*` entries — means the
venv predates the rename: rebuild it as above rather than installing over it.

## Optional environment

Copy `.env.example` to `.env` for local overrides. The **web app never
auto-logs-in from `.env`** — those credentials are read by the headless MCP
server only. Log in through the web UI; the session and keychain entry are
then reused.
