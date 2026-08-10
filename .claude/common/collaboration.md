# Collaboration standards (project-agnostic)

How work gets reviewed, merged, tracked and released. Copy verbatim into
another repository; project mechanics live in `../project/invariants.md`.

## Merging other people's work

**Never squash a pull request you did not write.** A squash collapses the
branch into one commit authored by whoever merged it, so the contributor
disappears from `git log`, from `git log --author`, and from the contributor
graph. No `Co-authored-by` trailer is added for you, and once released the
history cannot simply be amended.

```bash
gh pr merge <n> --merge      # anything you did not write
gh pr merge <n> --squash     # only your own
git log origin/main --author="<them>" --format='%h %an %s'   # verify
```

**Review changes go in a separate commit on top of theirs.** The difference
between what was contributed and what shipped stays legible, and their commit
stays theirs. Cherry-pick preserves authorship; `--amend` destroys it.

**Bring the mainline into a stale branch; do not rebase the branch.** Rebasing
rewrites their commits and usually needs a force-push, which is often
unavailable on a fork. A branch built as a *descendant* of their commit pushes
as a fast-forward. See `../skills/merge-a-contribution`.

## Reviewing

Separate three verdicts and never blur them: **must change** (breaks an
invariant or is unsafe), **would change** (your preference — say so, and let it
go), **accepting** (name what is good and why you left it alone).

Judge intent. A change that looks wrong often encodes a constraint you cannot
see. Say what evidence would convince you rather than asserting they are wrong.

Be concrete about consequences — "on a 500-item account that is 50 requests
before anything happens" — not adjectives. Name the specific thing they got
right; "nice work" tells them nothing about which instincts to keep.

## Rewriting shared history

**Prove every step is permitted before starting.** Dry-run the pushes. A
multi-step operation abandoned halfway leaves the mainline broken, and on a
released project that is an outage rather than untidiness.

If you cannot finish, restore first and explain second.

## The issue tracker is the project's memory

It is only worth reading if someone keeps it honest.

- Every issue gets **one type, one area, one priority, and a milestone**.
  There is no such thing as a correctly-unassigned issue.
- **Milestones are themes, not version numbers** — versions expire, themes
  accumulate meaning.
- Record **options considered and why the losing ones lost**. This is what
  stops the same debate reopening in three months.
- Write about the **problem, not the person**: no handles, no email addresses,
  no "as discussed with". Issues are public and long-lived.
- Close things for being *done, duplicated, or out of scope* — never for being
  old or hard. Say which, in a sentence. Closure has to mean something.

## Releasing

Tag only after the full gate: clean tree, green CI on the merge commit,
versions consistent across every manifest, security alerts triaged, docs
re-read against what actually shipped, and the feature exercised in the real
application. See `../skills/verify-before-release`.

Prefer rolling forward to moving a published tag. A moved tag means two
different trees answer to one version.
