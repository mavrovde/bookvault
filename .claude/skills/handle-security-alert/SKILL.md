---
name: handle-security-alert
description: Triage a static-analysis or dependency security alert — decide whether it is real, fix it at the right layer, and know when a suppression is the honest answer. Use for CodeQL, Dependabot, or any scanner finding.
---

# Handling a security alert

Portable: nothing here is specific to one project.

## 1. Decide whether it is real, in the app's actual threat model

A scanner reports a *pattern*, not an exploit. Work out the concrete path:
who supplies the input, what they can reach, and what they gain.

Write the answer down before touching code. "User-controlled path reaches a
filesystem write, and the endpoint is reachable cross-origin because the app
has no CSRF token" is a threat model. "CodeQL flagged line 40" is not.

Be honest in both directions. A "local-only app" is not automatically safe —
a browser will happily POST to `localhost` from any page the user has open.
Equally, a finding in test-only code usually is noise.

## 2. Fix at the layer that closes the class

Ask what else has the same shape. If one endpoint leaks an exception, they all
probably do; if one path is unconstrained, the missing control is usually
authentication or origin checking, not that one path.

Fix the class where you can, and when you can only bound the symptom, **say so
explicitly** and file the real fix. A patch that quiets a scanner while leaving
the class open is worse than none, because it stops anyone looking again.

## 3. Know the difference between normalising and constraining

A recurring trap with path handling:

- **Normalising** (`resolve()`, collapsing `..`, following symlinks) makes the
  value you checked the same as the value you use. Necessary, not sufficient.
- **Constraining** (the result must sit under an allowed root) bounds what can
  be reached at all.

Normalising alone stops traversal tricks but not "write into a directory that
happens to be dangerous". Do both, in that order — normalise first, then
compare against a fixed set.

## 4. Write the guard where the analyser can see it

Dataflow analysis follows values through a function, not through your custom
predicates. A perfectly good check hidden behind `if not _is_allowed(path):`
will often keep reporting, because the tool cannot tell that the helper
constrains anything.

Inline the comparison at the point of use, with a comment saying why. This is
usually clearer for human readers too — the guarantee is visible where the
value is consumed.

## 5. Suppress only with a written justification

Sometimes the code is genuinely correct and the tool cannot see it. Dismissing
is then legitimate — but:

- Dismiss the specific alert with a reason, in the tool, where the next person
  will find it. Never blanket-disable a rule in config.
- Say what makes it safe, not that it is safe.
- If you find yourself dismissing the same rule repeatedly, the design is
  fighting the tool. Revisit the design.

## 6. Prove it, then verify upstream

Add a regression test per finding: the malicious input, and the specific
refusal. Then confirm the scanner re-ran **on the commit that contains the
fix** — alerts stay open against the old analysis and it is easy to believe a
fix landed when the scan simply has not caught up.

```bash
gh api repos/<owner>/<repo>/code-scanning/analyses --jq '.[0].commit_sha'
gh api repos/<owner>/<repo>/code-scanning/alerts --paginate \
  -q '.[] | select(.state=="open") | "\(.number) \(.rule.id) \(.most_recent_instance.location.path)"'
gh api repos/<owner>/<repo>/dependabot/alerts --paginate \
  -q '.[] | select(.state=="open") | "\(.security_advisory.severity) \(.dependency.package.name)"'
```

## 7. Tell users what changed

If a fix narrows what was previously allowed, that is a behaviour change. It
belongs in the changelog in the user's terms — what they can no longer do, and
what to do instead — not buried as "hardening".
