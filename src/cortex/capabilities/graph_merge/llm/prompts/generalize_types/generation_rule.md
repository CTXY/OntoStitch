For GenerationRule knowledge, preserve every supplied effective field as one internally consistent
applicability contract. Equivalent mode canonicalizes
presentation only. Compatible mode may parameterize evidence-supported condition variants, but it
must update the reference closure to match the generalized rule: retain a concrete Concept or Metric
reference only when the generalized condition, action, or postcondition still depends on that exact
asset. When evidence supports a rule over an arbitrary grouping field or measure, do not carry over
references that belonged only to one concrete example. Conversely, never drop a reference whose
semantics remain required by the generalized behavior. Overlapping conditions with contradictory
actions or observable outcomes are conflicts and cannot be generalized. Never invent a property
that is absent from the supplied effective field definitions.

The column counted by an aggregation is part of its observable action, not an arbitrary measure.
Do not replace differing counted columns with an unbound “entity identifier” role. Without a
request-visible or stable runtime selector for that column, return `unable`.

Preserve exact SQL operator semantics. In particular, `RANK()` gives equal values the same rank and
then skips subsequent rank numbers; `DENSE_RANK()` does not skip them, while `ROW_NUMBER()` assigns
distinct row numbers. Never describe `RANK()` as not skipping ranks. If a source explanation says
otherwise but its action and evidence SQL use `RANK()` for top-N-with-ties behavior, correct the
explanation without changing the executable action.

Do not invent an executable alternative for an unobserved branch. If the shared semantics require
`RANK()`, a conditional residual about explicit tie language may reinforce that choice but cannot
weaken the default to `ROW_NUMBER()` or another operator unless supplied evidence positively permits
that alternative under a reusable condition.
