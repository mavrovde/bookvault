# Claude Code configuration

Checked-in harness config so every contributor gets the same setup.

- **`agents/`** — [subagent](https://docs.claude.com/en/docs/claude-code/sub-agents)
  definitions, one per area of the codebase. Each carries the invariants for
  its area: the things that aren't obvious from the code and that a fresh
  context will otherwise get wrong.
- **`skills/`** — the repeatable workflows: `dev-setup`, `run-app`,
  `add-a-pref`, `release`, `build-installers`.
- **`settings.json`** — an allowlist for routine read-only commands, a deny
  list covering the secret files, and a ruff hook that runs after edits.
  `settings.local.json` is git-ignored for per-machine overrides.

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
