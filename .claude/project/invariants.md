# BookVault invariants (project-specific)

The things a fresh context cannot infer and will otherwise get wrong. These
override anything in `../common/` where they disagree.

Ordered by how often they get violated.

## 1. One dedicated Playwright worker thread

Playwright's *sync* API is bound to the thread that created it, so
`core/bookvault_core/session.py` funnels **every** call touching a
`LitresClient` through one worker (`session.run` / `submit` / `run_async`).

There is exactly one logged-in account and one browser at a time. Never call
the client from a route handler, a background thread, or a test body. Code
already running *inside* a submission must not submit again — the single worker
would wait on itself and deadlock (see the MCP server's threading note).

Everything else follows from this: only one activity can run at a time, which
is why `activity.py` is a state machine rather than a job queue.

## 2. Request cadence, not volume

litres.ru sits behind an anti-bot layer that keys on **request count and
rhythm**. This is why the on-load size sweep is cache-only, why `iter_library`
jitters between pages, and why autosync runs on a multi-hour interval with
jitter rather than a tidy timer.

Treat as suspect: anything adding requests per book, lowering a page size,
polling on a fixed period, or scanning the whole library to find one item.
A larger response is cheaper than more responses.

A **bare 403 is a rights-limited title** — surface it as a skip. A 403/429/503
carrying a DDoS-Guard signature is transient — retry with backoff. Conflating
them either hammers the service or silently drops books the user owns.

## 3. Tests are offline and fully mocked

No test may start a browser or touch the network. `tests/conftest.py` fakes the
keyring, redirects state/cache/session files into a `tmp_path`, and resets
module-level singletons around every test.

Any new on-disk path — or a default pointing at a real user directory — must be
redirected there, or the suite writes into the developer's own files.

Env vars are read **at import** into module-level constants, so tests
monkeypatch the attribute, not the environment.

## 4. Durable activity state

`results`, `zip_path` and `saved_path` deliberately survive `_begin()`, so a
finished build's results view and download link outlive the size-check that
fires on the next page load. Adding state means deciding explicitly which side
of that line it is on, and saying so in a comment.

The zip is staged in a temp workdir and moved to its destination only on
success. Cleanup tracks `_state["workdir"]` — **never** derive the temp dir
from `Path(zip_path).parent`, because once saved that parent is the user's own
folder.

## 5. Naming is deliberate

`LITRES_*` environment variables, the `Litres*` class names and the
`.litres_*` data files are nominative references to the *service*, kept through
the litres-assistant → bookvault rename. Do not "fix" them to `BOOKVAULT_*`.

## 6. Secrets

`.env`, `.litres_session.json`, `.litres_cache.json` and `.litres_state.json`
are git-ignored and never committed, logged, or pasted into an issue. **Treat
the session file as being logged in** — it is a bearer credential. Never log a
password, a cookie, or the captured app-level headers.

## 7. Local by design, which is also a threat model

The web app binds `127.0.0.1` with no authentication and no CSRF token,
deliberately — it is a single-user tool. The consequence is that **any page the
user has open can POST to it**. Weigh that when adding a state-changing route,
and keep user-supplied paths constrained rather than merely normalised.

## 8. Scope

This tool downloads only what the account owner has already purchased, using
their own authenticated session. Anything that would reach other content is out
of scope and must be refused.

## 9. Release mechanics

Versions bump in lockstep across **all five** `pyproject.toml` files (root,
core, web, mcp, desktop). A pushed `v*` tag publishes the Docker images and all
three desktop installers. All builds are unsigned; do not claim otherwise.
