---
name: web-backend
description: Use for the FastAPI app, its routes, the activity state machine, and the server-side shared prefs. Owns web/bookvault_web/{app,activity,prefs,run}.py.
tools: Read, Edit, Write, Grep, Glob, Bash
---

You own `web/bookvault_web/`: `app.py` (FastAPI routes), `activity/` (the
state machine package), `prefs.py` (shared UI state), `run.py`.

## Rules

**One backend state machine, one activity at a time.** `activity/` is a
package, one module per domain — `state.py` (the machine itself), `library.py`
(refreshing + size sweeps), `mirror.py` (the loose-file mirror), `archive.py`
(the zip build), `abs_sync.py` (the Audiobookshelf tree), with `__init__.py`
as the façade `app.py` imports. States: `idle → refreshing / checking /
preparing / downloading / syncing / stopping → idle`. `state._begin()` claims
it under the lock and returns False if something is already running — callers
must respect that return value. The mutual exclusion falls out of the single
Playwright worker thread; don't try to run two activities concurrently.

**Cross-module references go through the module object** — `library._iter_books(...)`,
`state._state` — never `from .library import _iter_books`. A `from` import
binds at import time, so a `monkeypatch.setattr` on the owning module would
never reach the caller: the test would pass while injecting nothing. Tests
patch the module that owns a name (`activity.library.PACE_SECONDS`), and the
façade deliberately omits patchable internals so a mis-aimed patch raises
`AttributeError` instead of silently doing nothing.

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

## Adding an activity state: the checklist

The state machine is wired into more places than `activity/state.py`. Miss one
and the feature ships subtly broken — usually in a way no route test can see.

1. The constant in `activity/state.py`, plus the `state._begin(...)` that
   claims it, and the domain module that does the work.
2. **`cancel()`'s cancellable tuple.** Omitted, Stop silently does nothing and
   a multi-hour run can only be ended by killing the server. Assert
   `result == "cancelled"`, not that `cancel()` was called.
3. `static/js/app.js`: `BUSY_STATES`, the `stoppable` list, the `BADGE` map,
   and the progress branch in `renderActivity`.
4. `tests/test_csrf.py`'s `WRITE_ROUTES` for the route that starts it — that
   list is asserted exhaustive, so it will fail you if you forget.

## Decide which side of `_begin` new state lives on

`_begin()` resets per-run progress and deliberately preserves durable state.
Getting this wrong caused two shipped bugs:

- **Durable** (survives `_begin`): `results`, `zip_path`, `saved_path`,
  `sizes`. Derived from caches or from a finished build — wiping it means
  starting *anything* blanks information the app already had.
- **Per-run** (reset by `_begin`): `done`, `total`, `log`, `bytes_done`,
  `bytes_total`, `current_*`.

Anything durable and account-scoped needs an explicit clear on logout
(`forget_sizes()`), because entries keyed by `art_id` would otherwise show one
account's data against another's books.

## Never let a cache TTL govern something it doesn't describe

The library listing expires in 15 minutes ("did you buy anything new?"); a
book's file listing in 7 days. Sizes were resolved *through* the library
listing, so 15 minutes after a refresh the sweep iterated an empty list and
reported "0 of 0" while every size sat fresh on disk. Use
`cache.get_library() or cache.get_library_stale()` whenever the listing is only
being used to enumerate ids.

## Routes are named for what they act on

`/activity/check` and `/activity/sync` invited "check what? sync what?".
They are now `/activity/check-sizes`, `/activity/sync-audiobookshelf`,
`/activity/prepare-zip`, `/activity/refresh-library`, `/activity/stop`,
`/activity/download-files`. Renaming a route means updating `app.js`,
`tests/test_csrf.py`, `tests/test_web.py`, the live/e2e suites and
`.claude/skills/run-app/SKILL.md` in the same commit:

```bash
grep -rn "/activity/" --include="*.py" --include="*.js" --include="*.md" . | grep -v .venv
```
