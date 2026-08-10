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
is why `activity/` is a state machine rather than a job queue.

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

No test may touch the network, and the **default** suite starts no browser.
`tests/conftest.py` fakes the keyring, redirects state/cache/session files into
a `tmp_path`, and resets module-level singletons around every test.

**The one exception is `tests/test_ui.py`**, marked `ui` and deselected by
default (`addopts = -m 'not live and not ui'`), which drives the rendered page
through Chromium. It exists because two defects shipped in v1.3.3 that 400
Python tests could not see -- a button nested in a `<label>` so its click never
fired, and a label describing a state that could never happen. Both lived
between the DOM and the JS, where no route test reaches.

It is still **offline**: the app is served in-process against
`FakeLitresClient`, the lifespan is off (so no session restore from a cookie
file or the keychain), the browser talks only to 127.0.0.1, and an autouse
fixture makes constructing a real `LitresClient` an assertion failure. "Never
touches litres.ru" is enforced there, not merely intended. CI runs it in its
own job -- the only one that installs the Chromium binary.

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

The web app binds `127.0.0.1` with no authentication and no login,
deliberately — it is a single-user tool. The consequence was that **any page
the user has open could POST to it**. Since 1.3.3 `block_cross_origin_writes`
(`app.py`) refuses state-changing verbs that a foreign page made, using
`Sec-Fetch-Site` with an `Origin`-vs-`Host` fallback; callers with neither
header (curl, the live tests, local scripts) are still allowed, because the
threat model is a *web page the user visited*, not code already running as
them.

That check is a floor, not a substitute for the rest: keep user-supplied paths
**constrained** rather than merely normalised, because a bug in the check
should not be the only thing standing between a POST and the filesystem.

**Adding a state-changing route means adding it to `WRITE_ROUTES` in
`tests/test_csrf.py`.** That list is asserted to be exhaustive against the
app's own routing table, so a forgotten route fails the suite rather than
quietly becoming reachable from any page.

## 8. Scope

This tool downloads only what the account owner has already purchased, using
their own authenticated session. Anything that would reach other content is out
of scope and must be refused.

## 9. Release mechanics

Versions bump in lockstep across **all five** `pyproject.toml` files (root,
core, web, mcp, desktop). A pushed `v*` tag publishes the Docker images and all
three desktop installers. All builds are unsigned; do not claim otherwise.

## 10. No route may answer with a bare 500

A failure the user can act on must reach them as a readable page or a clean
JSON error, never Starlette's plain-text "Internal Server Error" — which loses
the form they were filling in and tells them nothing. Two rules follow:

- **Convert infrastructure failures into typed errors at the layer that knows
  what they mean.** `LitresClient.__init__` raises `LitresBrowserUnavailable`
  (a `LitresAuthError` subclass, so existing callers already degrade to
  "logged out") rather than letting Playwright's driver message escape.
- **Every route that can fail has a catch-all** that logs the traceback with
  `logger.exception` and returns a fixed, non-derived message. Nothing taken
  from an exception goes into a response body — that is how filesystem paths
  and stack traces leak (CodeQL `py/stack-trace-exposure`).

Unattended startup paths (`restore_session`) must degrade to "logged out"
rather than crash the boot: build the client **inside** the guard, or the app
cannot even show the login form explaining what went wrong.

## 11. A subprocess does not inherit the suite's fakes

`tests/conftest.py`'s autouse fixtures patch *this* process. A test that spawns
a real server (`test_e2e_smoke.py`) gets a fresh interpreter with a real
`keyring`, so on a machine that has ever signed in it will silently restore
that session and test the wrong page. Spawned processes must pin
`PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring` alongside the
`LITRES_*_FILE` overrides. A test that only passes on a machine that has never
logged in is not offline — it is lucky.

## 12. The browser cannot give you a filesystem path

`<input webkitdirectory>` exposes relative names and `showDirectoryPicker()` a
sandboxed handle; neither yields an absolute path, by design. Anything needing
a real directory is picked by a **native dialog opened server-side**
(`folder_dialog.py`) — legitimate here only because the server runs on the
user's own machine. When shelling out to one, the start path travels as an
argv element or an environment variable, never interpolated into a script
body, and the result is re-validated by the same guard a typed path goes
through.

## 13. A mode is wired into more places than the file you are editing

Adding a state to the activity machine, a status to a result list, or an option
to a pref means touching every place the existing siblings appear. The failure
mode is silent: the feature works, and one control adjacent to it quietly
doesn't.

Find the places by grepping for a sibling that already works, and treat every
hit as a site to update:

```bash
grep -rn "PREPARING\|preparing" web/bookvault_web/ --include="*.py" --include="*.js"
```

For a long-running state that means at least: the constant, the guard that
claims it, the cancel predicate, the frontend's busy/stoppable/badge maps, the
progress branch, and the route list in `tests/test_csrf.py`.

**Test the negative path first.** A control that silently does nothing —
a Stop that doesn't stop, a filter that matches nothing — passes every test
that only asserts the happy path. Assert the *effect* ("the run ended
cancelled"), never that the call was made.

## 14. Resumable work must verify content, not presence

Any operation that can be re-run has to decide "is this already done?", and the
cheap answer — the path exists — is always wrong. Work interrupted halfway
leaves something at the destination, and trusting it means the damage is
permanent and invisible.

- **Measure the property before you build a check on it.** A remote source
  asserting a size, a hash, or a version is making a claim, not a promise, and
  a claim that turns out not to describe what it delivers makes the check fire
  *always* rather than never — which presents as re-doing all the work every
  run. One throwaway script over real data settles it in minutes; shipping it
  costs a re-download of someone's entire library.
- Prefer the strongest evidence that is actually *true*. Comparing bytes on
  disk against an upstream number is the most direct check available and is the
  right instinct — but only once that number is known to describe the bytes.
  Where it doesn't, record what the finished run itself wrote and compare
  against that: weaker in principle, sound in practice.
- Record enough that partial *removal* is caught too, not just partial writing
  — a count for something stored unpacked, a length for a single file.
- **No record must not mean "redo it".** Absence of evidence is not evidence of
  damage. Something the user placed there themselves has nothing to verify
  against, and re-fetching it is destructive; leave it alone.
- **Write to a temporary name and rename on success.** A dead run must not
  leave wreckage where the finished artefact belongs, or the next run inspects
  the wreckage.
- **One function answers the question, and every caller uses it.** The mirror,
  the badge, and the zip's reuse are the same question about the same file.
  Implemented separately they drift, and the drift is silent: each is
  self-consistent, and the contradiction only shows in behaviour.

## 15. Look for the feature before building it

This codebase has several paths that look alike from outside, and a feature can
be invisible simply because its entry point is hidden behind an env var or a
capability check. Before designing anything, search for what already exists —
by capability, not by the name you would have given it:

```bash
grep -rn "def start_\|def prepare\|def sync\|def download" web/bookvault_web/activity.py
grep -rn "display:none\|{% if " web/bookvault_web/templates/index.html
```

A hidden entry point is not a missing feature. Surfacing or renaming the
existing one beats adding a near-duplicate, which confuses users permanently
and doubles the surface that has to stay correct.

## 16. A fake must model the service, not the code's hopes

The suite is fully offline, so `tests/fakes.py` *is* litres.ru as far as every
test is concerned. Whatever the fake asserts about the service becomes
unfalsifiable: production code and test agree, the suite is green, and the
disagreement only surfaces in a real library.

This has already cost a whole feature. The fake wrote exactly as many bytes as
a file listing declared, justified in its own comment as "a real download
produces a file matching the listed size". litres.ru does not, and the check
built on that belief would have re-downloaded every book on every run.

- When a fake encodes a **belief about the service** — a size, an ordering, a
  status code, a header — verify the belief against reality first, and write
  what you measured (with numbers) in the comment. A comment that argues from
  plausibility is a warning sign; it means nobody checked.
- Where the service is unreliable, make the fake unreliable **by default**, so
  code that depends on the reliability fails loudly here. Reserve the
  well-behaved case for a parameter an individual test opts into.
- Add a test that pins the misbehaviour (`test_the_fake_does_not_deliver_the
  _size_the_listing_declares`), so a later "cleanup" restoring the tidy
  version fails instead of quietly re-enabling the bug.

## 17. Seeded state proves less than a round trip

A test that writes the expected state by hand and then asserts one consumer
reads it back mostly tests the seed. If the seeded value embodies the same
wrong assumption as the code, it passes forever — this is how §16's bug
survived a large, careful suite.

For anything re-runnable, add at least one test that **performs the real
operation and then asks the question the app asks next**:

- Run it twice: the second run must do nothing. That single test would have
  caught the entire download-mirror failure.
- After one real run, query *every* consumer of that state (in this codebase:
  the run's own log, `books_on_disk`, `_local_copy_for`, the MCP tool). Drift
  between them is invisible to per-consumer tests.
- To check a test can actually fail, reintroduce the bug and watch it go red.
  A test that passes against the broken version is decoration.

## 18. Patch the module that owns the name

`web/bookvault_web/activity/` is a package with a façade `__init__.py`. Two
rules keep that structure testable, and both exist because breaking them fails
*silently*:

- **Cross-module references go through the module object** —
  `library._iter_books(...)`, `state._state`, not
  `from .library import _iter_books`. A `from` import binds the object at
  import time, so a later `monkeypatch.setattr` on the owning module is never
  seen by the caller: the test injects nothing and passes anyway.
- **Tests patch the owning module** (`activity.library.PACE_SECONDS`), not the
  façade. Rebinding a name on `__init__.py` changes only its own copy.

The façade therefore re-exports the *public* surface only. Patchable internals
are deliberately absent from it, so a mis-aimed `monkeypatch.setattr` raises
`AttributeError` — loud and immediate — rather than quietly binding a copy
nothing reads. When adding to the façade, ask whether a test might want to
patch the name; if so, leave it off.

## 19. The catalogue's *type* does not tell you the file's *shape*

`is_audio` says the title is an audiobook. It says nothing about what
litres.ru actually serves: only `zip_with_mp3` is a zip of tracks to unpack,
while `mobile_version_mp4` and friends are a single file, exactly like an
ebook. Branching on the type instead of the bytes broke every single-file
audiobook with "File is not a zip file" — an entire format, for every user who
prefers it.

- **Decide by inspecting the artefact** (`zipfile.is_zipfile(path)`), after it
  has arrived — never by a category from the listing.
- The guard is `is_audio AND is a zip`, never either alone: epub and fb2.zip
  *are* zips, and unpacking them would shred every ebook into loose files.
- One book therefore has two possible on-disk shapes. Anything asking "do I
  have this?" must accept both, and the record of what was written (`tracks`
  vs `size`) is what says which one to check.
- When a new download lands in the other shape, remove the stale one, or the
  mirror holds two copies and the older still looks like the book.

More generally: this codebase already had two correct implementations of this
exact decision (`library_fs.install_book` and the zip build, both probing
`is_zipfile`) when a third was written that guessed instead. Before writing a
rule about what the service delivers, grep for how the existing paths decide
it — see also §14 and §15.
