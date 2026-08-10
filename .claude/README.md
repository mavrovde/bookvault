# Claude Code configuration

Checked-in harness config so every contributor gets the same setup.

- **`agents/`** — [subagent](https://docs.claude.com/en/docs/claude-code/sub-agents)
  definitions, one per area of the codebase. Each carries the invariants for
  its area: the things that aren't obvious from the code and that a fresh
  context will otherwise get wrong.
- **`skills/`** — the repeatable workflows. Project-specific: `dev-setup`,
  `run-app`, `add-a-pref`, `release`, `build-installers`. Portable to any
  repo: `merge-a-contribution`, `handle-security-alert`,
  `verify-before-release`.
- **`common/`** — standards that hold in any repository: `engineering.md`
  (correctness, failure handling, concurrency, change hygiene) and
  `collaboration.md` (merging, review, history rewrites, the tracker,
  releasing). Copy these verbatim into another project.
- **`project/invariants.md`** — what makes *this* codebase different, ordered
  by how often each rule gets violated. This is the file to replace when
  porting.
- **`settings.json`** — an allowlist for routine read-only commands, a deny
  list covering the secret files, and a ruff hook that runs after edits.
  `settings.local.json` is git-ignored for per-machine overrides.

The first seven map to areas of the codebase; the last five are roles in the
workflow around it — review, verification, research, planning, triage.

The agent files double as a map of the codebase's seams for humans. If you're
new here, read `agents/web-backend.md` and `agents/core-client.md` first:
between them they cover the two decisions everything else follows from (one
Playwright worker thread, one activity at a time).

## Which agent owns what

| Task | Agent |
|---|---|
| Login, downloads, anti-bot, retries, the session/keychain/cache | `core-client` |
| FastAPI routes, the activity state machine, shared prefs | `web-backend` |
| The template, `app.js`, the stylesheet | `web-frontend` |
| Writing or fixing tests; anything that must stay offline | `test-guardian` |
| Versions, installers, Docker, CI workflows | `packaging-release` |
| Secret hygiene, exception exposure, unsafe file handling | `security-reviewer` |
| README / CLAUDE.md / CHANGELOG / `.env.example` | `docs-keeper` |
| Reviewing a PR you didn't write; deciding what to change vs accept | `pr-reviewer` |
| Verifying a change in the running app (UI, MCP tools, edge cases) | `qa` |
| Investigating an unknown before code gets written | `researcher` |
| Turning a request into a scoped issue with acceptance criteria | `story-writer` |
| Labelling, milestoning and sweeping the issue tracker | `triage` |

## Conventions they all share

- `LITRES_*` env vars and `Litres*` class names are **deliberate** references to
  the litres.ru service, kept through the litres-assistant → bookvault rename.
  Don't "fix" them to `BOOKVAULT_*`.
- Env vars are read **at import** into module-level constants, so tests
  monkeypatch the attribute rather than the environment.
- `.env`, `.litres_session.json`, `.litres_cache.json` and `.litres_state.json`
  are secrets. Never commit, print, or log them.
- The test suite is fully offline and mocked. No test starts a browser or hits
  the network.

## Adding a role

Keep it short and specific to *this* repo. Generic advice ("write clean code")
costs context and earns nothing — the value is in the constraints that are
expensive to rediscover.


## How the pieces compose

```
common/engineering.md  +  common/collaboration.md     <- portable, any repo
            +
project/invariants.md                                 <- this codebase only
            +
agents/*.md  ·  skills/*/SKILL.md                     <- roles and workflows
```

Read the two `common/` files as the baseline, then `project/invariants.md`,
which **overrides** the baseline wherever they disagree. Agents and skills
build on both: an agent describes a role and points at the constraints rather
than restating them, so a rule is written once and cannot drift between twelve
copies.

`CLAUDE.md` at the repo root remains the short, always-loaded summary; these
files are the long form for when an agent needs the detail.

## Reusing this configuration elsewhere

Roughly half of this is portable. The split is deliberate, so a copy into
another repo is a small edit rather than a rewrite.

**Take as-is** — these encode process, not this codebase:

| File | What it carries |
|---|---|
| `skills/merge-a-contribution` | Never squash someone else's PR; how to revive a stale fork PR with a fast-forward |
| `skills/handle-security-alert` | Threat-model a finding, fix the class, normalise *and* constrain, when to suppress |
| `skills/verify-before-release` | The pre-tag gate, in dependency order |
| `common/engineering.md`, `common/collaboration.md` | The shared baseline |
| `agents/pr-reviewer`, `qa`, `researcher`, `story-writer`, `triage` | Workflow roles |

**Replace** — these are about *this* codebase:

| File | Substitute |
|---|---|
| `agents/core-client`, `web-backend`, `web-frontend` | Your own module boundaries |
| `agents/packaging-release` | Your build and release mechanics |
| `agents/test-guardian`, `docs-keeper` | Your test invariants and doc layout |
| `skills/dev-setup`, `run-app`, `add-a-pref`, `release`, `build-installers` | Your commands |
| `project/invariants.md` | **Your** load-bearing constraints — the highest-value file to write |
| `settings.json` | Your tool allowlist and hooks |

The workflow agents still reference project invariants — the single Playwright
worker thread, request cadence, offline tests. When porting, swap that list for
your own: the *shape* ("here are the things a newcomer cannot know, in the
order they get violated") is what transfers.

## What makes these worth keeping

Every rule here cost something to learn. A squash merge that erased a
contributor's authorship. A `resolve()` that normalised a path but did not
constrain it. A revert pushed to `main` before checking the follow-up was even
possible. Keep the *why* attached to each rule — a rule with its reasoning
survives a rewrite; a bare instruction gets optimised away by the next person
who thinks they know better.

Generic advice ("write clean code", "add tests") costs context and earns
nothing. If a line would be true of any repository, delete it.
