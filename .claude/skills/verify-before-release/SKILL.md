---
name: verify-before-release
description: The gate to run before tagging any release — tests, lint, security alerts, docs drift, version consistency, and a real exercise of the app. Use immediately before cutting a version.
---

# Verifying a release

Portable: the checks are generic, the commands are per-project. Substitute
yours and keep the order — each step is cheap relative to the one after it.

## 1. The branch you are tagging is the branch you tested

```bash
git status --porcelain     # must be empty
git log --oneline -1       # is this actually what you think it is?
```

Tagging a dirty tree ships something no CI run ever saw.

## 2. Automated gates

```bash
<test command>             # full suite, no skips you did not intend
<lint command>
```

Then confirm CI is green **on the merge commit**, not on the PR branch — a
merge can break what both sides passed independently.

## 3. Version consistency

Multi-package repos drift silently. Assert every manifest agrees:

```bash
grep -h -m1 '^version' <every manifest> | sort -u    # must print one line
```

## 4. Security alerts

Check the scanner has run **on the commit you are about to tag**, and that
nothing is open. An alert dismissed with a justification is fine; an alert
nobody has looked at is not. See the `handle-security-alert` skill.

## 5. Documentation drift

The two things that always rot:

- **The feature list and the getting-started steps.** Re-read them against
  what actually shipped this cycle.
- **Configuration.** Every new environment variable or setting must appear
  everywhere it is documented, and anywhere the packaged build needs to set it.

Then write the changelog entry in the user's terms: what changed for them, and
why if it is not obvious. If a fix narrows previously-allowed behaviour, say
so plainly.

## 6. Exercise the real thing

Automated tests are usually mocked, so run the app and use the feature the
release is named after. Especially anything with no test coverage by nature —
UI, packaging, installers. See the `qa` agent.

## 7. Tag, then watch

Push the tag only after the above. Then confirm the publish workflows finished
**and that the artifacts actually attached** — a build can succeed and still
fail to upload. Check the release page, not just the workflow status.

## 8. Verify the published artifacts as a user would

Download at least one artifact from the release page and check it the way the
README tells a stranger to. Provenance and checksums are only worth publishing
if they actually validate:

```bash
gh attestation verify <artifact> --repo <owner>/<repo>
sha256sum -c SHA256SUMS
```

If a release publishes checksums covering several artifacts built by separate
workflows, confirm the checksum file lists **all** of them — a collector that
runs while a slow build is still going will happily publish a partial file.

## If something is wrong after tagging

Prefer rolling forward with a new patch version over deleting or moving a tag.
Published tags may already be fetched, pinned, or cached by others; a moved tag
means two different trees answer to one version.
