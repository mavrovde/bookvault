---
name: add-a-pref
description: Add a new shared UI preference (server-side, persisted, synced across browsers) to the BookVault web app. Use when adding any setting the user changes in the UI and expects to persist.
---

# Adding a shared preference

Prefs live on the **server** (`web/bookvault_web/prefs.py`), persisted to
`LITRES_STATE_FILE`, and are folded into the `/activity` poll so every browser
and tab converges on the same value. They are not per-browser storage.

Adding one touches **five** places. Missing any of them produces a pref that
half-works — usually one that saves but doesn't survive a reload, or one that
leaks between tests.

## 1. `web/bookvault_web/prefs.py`

- Add the key to `_DEFAULTS`.
- Return it from `snapshot()` (the dict is enumerated explicitly, not
  `dict(state)` — unknown keys are deliberately dropped by `_load()`).
- Add a keyword parameter to `update()` and set it when not `None`.

**`None` means "leave this field alone."** That's the partial-update contract
the frontend relies on when it pushes one field at a time. If your pref needs
to be *clearable*, pick a separate sentinel — `download_dir=""` resets to the
default. Validate **before** taking the lock so a rejected value can't leave a
multi-field update half-applied.

If the value needs a fallback chain (user choice → env var → system default),
put the resolution in one function in `prefs.py` and expose the resolved value
as a derived `*_effective` key so the UI doesn't reimplement it.

## 2. `web/bookvault_web/app.py`

- Add the field to `PrefsUpdate`.
- Pass it through in `set_prefs`.
- If it can be invalid, raise a typed exception carrying a **code** and have
  the route look the message up from a constant table — never return
  `str(exc)`, which CodeQL flags as stack-trace exposure. See
  `InvalidDownloadDir` / `DOWNLOAD_DIR_ERRORS`.
- Add it to the template context in `index()` so the control server-renders
  with its saved value and doesn't flash empty.

## 3. `web/bookvault_web/templates/index.html`

Add the control in `.format-controls` (topbar) with a `title=` explaining what
it does. Give it an id; server-render `value`/`selected` from the context.

## 4. `web/bookvault_web/static/js/app.js`

- Hydrate it in `applyPrefs()` — and **skip hydration while a local push is in
  flight or the field has focus**, or a poll will overwrite what the user is
  typing (`selectionPushPending` / `downloadDirPending`).
- Push it: `pushFormat`-style (immediate) for a select, debounced for free
  text. Handle a non-OK response by showing the error inline.
- Wire it up in `initPrefControls()`.

## 5. `tests/conftest.py`

If the pref introduces a new module-level default that points anywhere real
(a home directory, a system folder), **monkeypatch it in
`isolated_module_state`** — otherwise the suite writes into the developer's
own files. Add any new `activity._state` key to `_reset()`.

## Then

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

Cover: the default, a set/get round-trip, persistence across a reload
(`prefs._state = None` simulates a fresh process), rejection of bad input over
HTTP, and cross-browser visibility (two `TestClient`s).
