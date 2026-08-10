# Engineering standards (project-agnostic)

Shared baseline. Nothing here mentions this repository — copy it verbatim into
another one. Project-specific rules live in `../project/invariants.md`, which
always wins where the two disagree.

## Correctness

**Fix the class, not the instance.** When a bug is found, ask what else has the
same shape. One endpoint leaking an exception usually means they all do.

**A guarantee that isn't tested is a guess.** Name tests as claims:
`test_a_new_build_never_deletes_the_users_folder` beats `test_prepare_2`. A
reader should learn the invariant from the name.

**Prefer observable assertions.** Assert on what a user or caller can see, not
on internal call sequences — except where the call count *is* the guarantee
("served from cache, no second fetch").

**Validate before mutating.** A rejected input must not leave a partially
applied change behind.

## Handling failure

**Never return raw exception text to a caller.** Raise a typed error carrying a
*code*; map it to a fixed, human-readable message at the boundary. Exception
text leaks internal and filesystem detail, and static analysers are right to
flag it. This applies to any attacker-visible output, including API and tool
results.

**Broad `except` is sometimes correct** — unattended startup, a long batch job
where one item must not sink the rest, best-effort cleanup. When it is,
annotate it in place with the reason. A rule that stays enabled everywhere else
is worth more than one disabled globally.

**Degrade rather than crash** on paths that run unattended. Log at a level that
matches how actionable it is: `debug` for expected-and-harmless, `warning` for
"a human might care", `exception` only where the stack is genuinely useful.

## Concurrency and external services

**Respect thread affinity.** Libraries bound to their creating thread must be
funnelled through the owner. Document the constraint where it is enforced, not
only where it is violated.

**Rate and rhythm matter more than volume.** Services that throttle or
challenge clients key on request *count and regularity*. Jitter periodic work;
prefer one large request to many small ones; never poll on an exact interval.

**Bound anything unattended.** Timers, retries and backoffs need a floor, a
ceiling, and a reason for each.

## Changes

**Explain why, not what.** The diff shows what changed. The message should say
what was wrong, what it would have cost, and why this approach over the
alternative.

**Match the surrounding code** — comment density, naming, idiom. A change that
reads as foreign is harder to review even when it is correct.

**Keep one concern per change.** If the description needs "and", it is
probably two.

**Leave the reasoning attached.** A rule without its rationale gets optimised
away by the next person who thinks they know better.

## Scope discipline

Deliver what was asked. If you find a real problem with the request, say so in
a sentence and then build it anyway under a stated assumption — narrowing scope
unilaterally is not a decision that belongs to the implementer.

When something is blocked, finish everything else and state plainly what was
left and why. A partial result reported as complete is worse than a failure.
