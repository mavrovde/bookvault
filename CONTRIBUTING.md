# Contributing to BookVault

Thanks for helping out. BookVault backs up a user's **own** purchased
litres.ru library, entirely from their machine. That framing decides a lot of
design questions, so it's worth keeping in mind: it's a personal, local,
single-user tool — not a service, and not a way to reach anything the user
hasn't bought.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ./core -e ./web -e ./mcp -e ".[dev]"
.venv/bin/playwright install chromium          # one-time, ~150 MB
```

Add `-e ./desktop` for the native window.

```bash
.venv/bin/python -m pytest        # full suite — offline, seconds
.venv/bin/python -m ruff check .  # CI-enforced
```

Prefer `.venv/bin/python -m <tool>` over the console scripts in `.venv/bin`:
those bake an absolute interpreter path into their shebang and break if the
repo is moved or renamed.

## The rules that matter

**Tests are offline and fully mocked.** No test may touch the network, and the
default suite starts no browser. `tests/conftest.py` fakes the keyring,
redirects every state/cache/session file into a `tmp_path`, and resets
module-level singletons around each test. If your change adds a new on-disk
path or a default pointing at a real user directory, redirect it there too —
otherwise the suite writes into the developer's own files.

One deliberate exception: `tests/test_ui.py` drives the rendered page through
Chromium, because a button can be perfectly wired server-side and still do
nothing when clicked. It's marked `ui` and deselected by default — run it with
`pytest -m ui`, after `playwright install chromium`; CI runs it in its own job.
It is still offline — the app is served against the fake client, and
constructing a real one is an assertion failure, so it can never reach
litres.ru.

**If you add a state-changing route, add it to `WRITE_ROUTES` in
`tests/test_csrf.py`.** That list is asserted to be exhaustive against the
app's own routing table, so a `POST` added without a thought for the
cross-origin guard fails the suite instead of quietly becoming reachable from
any page a user happens to have open.

**One Playwright worker thread.** Playwright's sync API is bound to the thread
that created it, so every call touching a `LitresClient` goes through
`session.run` / `session.submit`. This is why only one activity can run at a
time. Don't call the client from a route handler or a new thread.

**`LITRES_*` names are deliberate.** The project was renamed
litres-assistant → bookvault, but the env-var prefix, the `Litres*` class
names, and the `.litres_*` data files were kept as references to the *service*.
Please don't "fix" them to `BOOKVAULT_*`.

**Secrets never get committed.** `.env`, `.litres_session.json`,
`.litres_cache.json`, `.litres_state.json` are git-ignored. Treat the session
file as being logged in — it's a bearer credential. Never log a password, a
cookie, or the captured app-level headers.

**Routes never return `str(exc)`.** Raise a typed exception carrying a *code*
and look the user-facing message up from a constant table (see
`prefs.InvalidDownloadDir`). CodeQL flags the alternative as stack-trace
exposure, and it genuinely leaks internal detail.

**Versions bump in lockstep** across all five `pyproject.toml` files (root,
core, web, mcp, desktop) when releasing.

## Pull requests

- Branch off `main`; one concern per PR.
- Include tests. Name them as claims —
  `test_a_new_build_never_deletes_the_users_download_folder` beats
  `test_prepare_2`.
- Update the docs in the same PR. A new `LITRES_*` variable belongs in
  `.env.example`, the README config table, `mcp/README.md` if the MCP server
  reads it, and `packaging/entry.py` if it's a path.
- Explain **why** in the commit message and PR body, not just what. The
  existing history and comments do this, and it's the reason the codebase is
  navigable.
- CI must be green: ruff, the 3.11–3.13 test matrix, the dependency audit, and
  CodeQL.

### How your PR gets merged

PRs from outside contributors are merged with a **merge commit**, never
squashed, so your commit keeps your name in `git log` and on the contributor
graph. Review changes are added as separate commits on top of yours rather
than folded into it, so the difference between what you wrote and what shipped
stays readable.

If your branch has gone stale, we bring `main` *into* your branch rather than
rebasing it, so your commit keeps its identity.

## AI-assisted contributions

Much of this repo is maintained with [Claude Code](https://claude.com/claude-code),
and the harness config is checked in so everyone gets the same setup:

- **`.claude/agents/`** — role definitions carrying each area's invariants
  (`core-client`, `web-backend`, `web-frontend`, `test-guardian`,
  `packaging-release`, `security-reviewer`, `docs-keeper`), plus roles for the
  workflow around the code (`pr-reviewer`, `qa`, `researcher`, `story-writer`,
  `triage`). See
  [`.claude/README.md`](.claude/README.md) for the task → agent map.
- **`.claude/skills/`** — the repeatable workflows: `dev-setup`, `run-app`,
  `add-a-pref`, `release`, `build-installers`.
- **`.claude/settings.json`** — an allowlist for the routine read-only commands
  and a ruff hook that runs after edits. `settings.local.json` is git-ignored
  for per-machine overrides.
- **`CLAUDE.md`** — the load-bearing architectural decisions.

AI-assisted PRs are welcome and held to exactly the same bar as any other:

- **A human reviews and signs off on every PR.** Generated code is a draft
  until a person has read it.
- **The author is accountable for the change** — including that they
  understand it, that it's tested, and that it doesn't quietly widen scope.
- **Ship the tests with it.** A change whose tests were written by the same
  pass that wrote the bug is worth extra scrutiny; prefer tests that assert an
  observable guarantee over ones that mirror the implementation.
- **Don't let a tool paper over a real decision.** If a lint rule, a type
  error, or a security finding is telling you something true about the design,
  fix the design rather than suppressing the signal. Where a suppression *is*
  right (the intentional broad `except` handlers in the anti-bot paths), it's
  annotated in place with its reason so the rule stays active everywhere else.

Disclose AI assistance in the PR if it did substantial work — a
`Co-Authored-By` trailer is enough.

## Reporting bugs

Include your OS, which front-end (web / desktop / MCP / Docker), and how you
installed it. **Never paste `.env` contents, cookies, or your session file** —
redact anything that looks like a credential.

## Scope

Please don't propose changes that reach content the user hasn't purchased,
bypass payment, or break DRM. This project makes personal backup copies of
already-owned purchases using the account owner's own authenticated session,
and that boundary isn't negotiable. See [`SECURITY.md`](SECURITY.md) for
reporting vulnerabilities.
