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
