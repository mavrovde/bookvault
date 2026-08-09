<!-- One concern per PR. Explain WHY, not just what. -->

## What & why

<!-- The problem this solves, and the outcome. If it fixes a bug, say what
     went wrong and what would have kept going wrong. -->

## How it behaves

<!-- Anything a reviewer couldn't infer from the diff: trade-offs made,
     edge cases handled, behaviour under failure. -->

## Testing

<!-- What you added, and how you verified it beyond the suite (ran the app?
     a real download? which front-end?). -->

- [ ] `.venv/bin/python -m pytest` passes
- [ ] `.venv/bin/python -m ruff check .` clean
- [ ] New behaviour has tests

## Checklist

- [ ] No secrets committed (`.env`, `.litres_session.json`, `.litres_cache.json`, `.litres_state.json`)
- [ ] Tests stay offline — nothing starts a real browser or hits the network
- [ ] New `LITRES_*` var? Added to `.env.example`, the README config table, `mcp/README.md` if the MCP server reads it, and `packaging/entry.py` if it's a path
- [ ] Docs updated (`README.md` / `CLAUDE.md` / `CHANGELOG.md`) if user-facing
- [ ] Releasing? Versions bumped in lockstep across all five `pyproject.toml` files

<!-- If AI assisted substantially, mention it (a Co-Authored-By trailer is
     enough). Same review bar either way — see CONTRIBUTING.md. -->
