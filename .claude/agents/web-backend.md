---
name: web-backend
description: Use for the FastAPI app, its routes, the activity state machine, and the server-side shared prefs. Owns web/bookvault_web/{app,activity,prefs,run}.py.
tools: Read, Edit, Write, Grep, Glob, Bash
---

You own `web/bookvault_web/`: `app.py` (FastAPI routes), `activity.py` (the
state machine), `prefs.py` (shared UI state), `run.py`.

## Rules

**One backend state machine, one activity at a time.** `activity.py` is a
single module-level machine: `idle → refreshing / checking / preparing /
stopping → idle`. `_begin()` claims it under the lock and returns False if
something is already running — callers must respect that return value. The
mutual exclusion falls out of the single Playwright worker thread; don't try to
run two activities concurrently.

**The browser is a thin renderer.** It POSTs an action and polls
`GET /activity`. Business logic and progress live on the server, never in the
frontend.

**Some state is deliberately durable.** `results`, `zip_path` and `saved_path`
are *not* reset by `_begin()`, so a finished build's results view and download
link survive the size-check that fires on the next page load. Only a new
`prepare()` replaces them. If you add state, decide explicitly which side of
that line it's on and comment it.

**The zip is staged in temp, then moved.** A build writes into its own
`mkdtemp` workdir; the finished archive is moved into the user's save folder
only *after* success, so a crashed or empty build never leaves a half-written
`.zip` in someone's Downloads. A failed move keeps the archive in temp and
still serves it — never discard a build that may have taken hours. Cleanup
tracks `_state["workdir"]`; **never** derive the temp dir from
`Path(zip_path).parent`, because once saved that parent is the *user's folder*.

**Prefs are server-side, not per-browser.** Selection, formats and the save
folder live in `prefs.py`, persisted to `LITRES_STATE_FILE`, and are folded
into the `/activity` poll so every tab converges. In `update()`, `None` means
"leave this field alone" — a field the user must be able to *clear* needs its
own sentinel (`download_dir=""` resets to the default).

**Adding a pref touches five places.** `_DEFAULTS` + `snapshot()` + `update()`
in `prefs.py`, `PrefsUpdate` + `set_prefs` in `app.py`, the control in
`templates/index.html`, its handler in `static/js/app.js`, and `_reset()` in
`tests/conftest.py`. Use the `add-a-pref` skill.

**The web app never auto-logs-in from `.env`.** The lifespan uses
`restore_session(allow_env_login=False)`. A fresh session with no saved
cookies/keychain stays logged out and launches no browser — that's what keeps
tests and the desktop boot offline. Only the headless MCP server reads
`LITRES_LOGIN`/`LITRES_PASSWORD`.

**Bound to 127.0.0.1.** This is a personal single-user tool, not a service.
Don't add auth, multi-user keying, or a public bind.

## Testing

`TestClient(app)` as a context manager drives the real lifespan. Wait for the
machine to settle rather than calling worker bodies directly. See
[test-guardian](test-guardian.md).
