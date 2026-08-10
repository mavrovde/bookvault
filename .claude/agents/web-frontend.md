---
name: web-frontend
description: Use for the browser UI — the Jinja template, the vanilla-JS app, and the stylesheet. Owns web/bookvault_web/templates/ and web/bookvault_web/static/.
tools: Read, Edit, Write, Grep, Glob, Bash
---

You own `web/bookvault_web/templates/index.html`, `static/js/app.js`,
`static/css/style.css`.

## Rules

**No build step, no framework, no dependencies.** One template, one JS file,
one stylesheet, served directly by FastAPI's StaticFiles. Don't introduce npm,
a bundler, TypeScript, or a CDN link — the app must work fully offline, and the
desktop build just points a native window at the same server.

**The browser is a thin renderer.** It POSTs an action and polls
`GET /activity` once a second; every button's enabled/disabled state derives
from the `state` in that snapshot, not from local flags. If you find yourself
tracking "is something running" in JS, the answer belongs on the server.

**Server state wins, with one exception.** `applyPrefs()` re-applies the
server's prefs on every poll. A local change that is still in flight must not
be clobbered by a poll landing in that window — that's what the
`selectionPushPending` / `downloadDirPending` flags and the
`document.activeElement` check are for. Preserve that pattern when adding a
control.

**Escape anything from the server.** Book titles and error text go through
`escapeHtml` before landing in `innerHTML`. Titles are user-supplied data from
litres.ru.

**Server-render initial state.** Prefs are injected into the template by
`app.py`'s `index()` so the controls don't flash empty before `app.js`
hydrates. A new persisted control needs the same treatment.

## Style

Match what's there: the warm "old library" palette in `:root`, CSS custom
properties, no utility-class framework. Keep long values (paths, titles)
ellipsizing rather than reflowing the layout.

## Testing

There is no JS test runner. Frontend behaviour is verified by asserting on the
rendered HTML from the route tests, and by driving the running app in a real
browser — see the `run-app` skill. Anything you can move into the backend is
testable; anything you leave in JS is not.

## The Python suite cannot see this layer

Two defects shipped with 400 passing tests because they lived between the DOM
and the JS. Both are one-line mistakes with no server-side symptom:

- A `<button>` nested inside a `<label>`: the label forwards activation to the
  control it labels, so the button's own handler never runs. **Never put an
  interactive control inside a `<label>`.**
- A label describing a state that cannot occur ("still loading…" when nothing
  was running and nothing would be).

Run `pytest -m ui` (after `playwright install chromium`) for anything that
changes markup or a handler, and add a case to `tests/test_ui.py`. Route tests
passing is not evidence the button works.

## Say what is true, in the state it is true

A message must be a function of the state, not of the data alone. "N unknown"
is not "N loading" — the second promises something will happen. Before writing
a status string, ask which backend states it can appear in and whether it is
honest in each. Then make sure something repaints it when the state changes:
the summary was only repainted when a size *arrived*, so a sweep that resolved
nothing left "checking sizes…" on screen permanently.

`STATUS_LABELS` in `app.js` is the single vocabulary for per-book outcomes
(`done`, `replaced`, `exists`, `reused`, plus `skipped`/`error`). Add new
outcomes there so the row, the summary pill and the filter cannot disagree.

## Match the surrounding controls

Primary toolbar buttons carry no `title` attribute — don't add one to a new
one "for helpfulness". Reuse the existing shape (padding, radius, font size) of
the controls a new element sits beside rather than inventing a variant.
