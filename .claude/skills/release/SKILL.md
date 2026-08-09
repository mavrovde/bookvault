---
name: release
description: Cut a BookVault release — bump versions in lockstep, update the changelog and docs, tag, and verify the publish workflows. Use when asked to release, cut a version, or ship.
---

# Cutting a release

A pushed `v*` tag is what publishes. Everything below happens **before** the
tag, because the workflows build from it.

## 1. Preconditions

- `main` is green (lint, the 3.11–3.13 test matrix, the dependency audit,
  CodeQL). Check before anything else — a tag on a red `main` publishes broken
  artifacts.
- Everything intended for this release is merged.
- Working tree clean, on `main`, up to date.

## 2. Bump the version in lockstep

**All five** `pyproject.toml` files — root, `core/`, `web/`, `mcp/`,
`desktop/`. Missing one produces an install whose subpackages disagree about
their own version.

```bash
grep -n '^version' pyproject.toml core/pyproject.toml web/pyproject.toml \
                   mcp/pyproject.toml desktop/pyproject.toml
```

All five must read the same string afterwards.

## 3. Update the docs

- `CHANGELOG.md` — promote `[Unreleased]` to the new version with a one-line
  summary of the release's theme, then Added / Changed / Fixed. Write for the
  user: what changed and why, not which functions moved.
- `README.md` — re-read the Features list and the "Using the web app" steps
  against what actually shipped. Those drift first.
- `.env.example` + the README config table — every new `LITRES_*` var.

Use the `docs-keeper` agent for this step.

## 4. Verify locally

```bash
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
```

## 5. Commit, tag, push

```bash
git commit -am "chore(release): vX.Y.Z"
git tag vX.Y.Z
git push origin main
git push origin vX.Y.Z          # this is what triggers publishing
```

## 6. Watch the four workflows

| Workflow | Produces |
|---|---|
| `docker-publish.yml` | multi-arch `ghcr.io/mavrovde/bookvault/{web,mcp}` |
| `desktop-macos.yml` | `BookVault.app` / `.dmg` |
| `desktop-windows.yml` | `Setup.exe` (Inno Setup) |
| `desktop-linux.yml` | `.AppImage` (+ headless xvfb smoke test) |

```bash
gh run list --limit 8
```

## 7. Check the Release

All three installers attached, and the notes match `CHANGELOG.md`. All builds
are **unsigned** — say so in the notes (macOS needs right-click → Open,
Windows shows SmartScreen). Windows needs the WebView2 runtime; Linux needs
WebKitGTK 4.1 on the host.
