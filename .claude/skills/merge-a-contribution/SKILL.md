---
name: merge-a-contribution
description: Merge an outside contributor's pull request without destroying their authorship. Use for ANY PR from someone other than the maintainer, especially a stale one from a fork that no longer merges cleanly.
---

# Merging someone else's contribution

A contributor's name in `git log` is the only durable record that they wrote
the code. Losing it is not a cosmetic problem — it is the one mistake in this
workflow that cannot be undone after the fact.

## The rule

**Never squash a PR authored by someone else.** Merge it (`--merge`), always.

A squash collapses every commit into one authored by whoever pressed the
button. The contributor disappears from `git log`, from `git log --author`,
and from GitHub's contributor graph. GitHub does *not* add a
`Co-authored-by` trailer for you.

```bash
gh pr merge <n> --merge     # contributor PRs -- preserves authorship
gh pr merge <n> --squash    # ONLY for your own PRs
```

Verify afterwards, every time:

```bash
git log origin/main --author="<their name>" --format='%h | %an | %s'
```

If that prints nothing, their authorship is gone.

## Reviewing without rewriting

Put your changes in a **separate commit on top of theirs**, never folded into
their commit. The diff between what they wrote and what shipped stays legible,
and their commit stays theirs. Cherry-pick preserves authorship; `--amend`
does not.

## A stale fork PR that no longer merges

The tempting fix — rebase their branch and force-push — is the wrong shape:
it rewrites their commit (new SHA, and it can silently re-attribute), and a
force-push to a fork is frequently blocked.

Do this instead. It keeps their commit byte-for-byte and needs only a
**fast-forward** push:

```bash
git remote add contributor https://github.com/<user>/<repo>.git
git fetch contributor <their-branch>

git checkout -b merge-prep <their-commit-sha>   # start AT their commit
git merge origin/main                            # bring main in; resolve conflicts
# ...apply your review changes as a further commit...

git merge-base --is-ancestor <their-commit-sha> HEAD   # must succeed
git push contributor merge-prep:<their-branch>          # fast-forward, no --force
```

Because the branch descends from their commit, the push is a fast-forward and
the PR becomes `MERGEABLE` with their commit intact.

Note the PR can show a non-empty "files changed" even when the net diff
against `main` is nothing — GitHub diffs from the merge base. Merging then
produces a merge commit with no tree change, which is fine: it records the
contribution without touching the code.

## Before rewriting published history

If a fix needs `git revert` on `main` plus a follow-up (a re-merge, a
force-push elsewhere), **prove every step is permitted before starting**. A
half-finished sequence leaves `main` broken.

```bash
git push --dry-run contributor HEAD:<their-branch>   # confirm access FIRST
```

Then: build and verify the end state locally (full suite + ruff), and only
then push, so `main` is never red between two commits. If you find yourself
pushing a revert you cannot yet complete — stop and restore instead.

## Say thank you, specifically

Name the thing they got right. "The zip-slip guard and writing metadata only
after the media lands" beats "nice work" — it shows the review was real, and
it tells them which instincts to keep.
