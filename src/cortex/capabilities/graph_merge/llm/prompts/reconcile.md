# Task

Analyze representations that already have the same identity. Determine whether overlapping runtime
contexts and triggers can activate behaviors that produce different observable results, and whether
one lossless canonical implementation can represent them. Do not reconsider identity and do not
generate canonical content; later stages fuse or generalize each accepted group.

Use `working_memory` to inspect previous analysis, candidates, independent reviews and failures.
For structural `contract_errors`, repair the listed contract. For semantic feedback, re-examine the
observations, difference explanations, applicability selectors and affected grouping. Withdraw or
revise difference points when new evidence contradicts them. Retain checked explanations unless
new evidence contradicts them. A checker classification requests investigation; it is not proof of
conflict. Do not force compatibility or infer conflict from generation failure.

If evidence cannot establish equivalence, applicability or competition, list the affected refs in
`unresolved_refs`, each in its own equivalent singleton group. Those refs must not appear in any
difference point. Difference points cover all resolved refs only. Multiple groups without a conflict
point are allowed only for explicitly unresolved singletons plus at most one resolved group.
Record the missing evidence in `reason`. Never invent a question or business selector.

# Analysis procedure

1. Read the supplied asset Schema and identify the smallest runtime context and trigger under which
   each representation applies. Context contains only facts visible before choosing the behavior.
2. Test context overlap: ask whether one plausible runtime request can satisfy two representations'
   triggers. Lexical similarity alone is insufficient, and different wording may still have the
   same applicability.
3. Within each overlapping context, compare the complete behaviors. Test behavior divergence by
   applying them to the same valid input and identifying the first observable result that can differ.
   SQL syntax or field text that cannot change a result is not a difference point.
4. Test for a selector already supplied by the request, Schema, selected data, or stable runtime
   context. The selector must be visible before the behavior and must actually separate every
   supporting source context.
5. Create a `difference_point` only when contexts overlap, behaviors can produce different results,
   and no valid selector separates them. Partition those positions into separate groups. Differences
   with disjoint contexts, equivalent results, safe parameterization, or a valid selector belong in
   `shared`/`residuals` and do not become difference points.
6. Check that every source meaning and behavior survives in exactly one group.

# Difference point contract

- A DifferencePoint contains only point_id, aspect, positions, resolution and reason. Do not add
  supporting_refs directly to a point; use positions[].members with representation_ref and
  source_case_ids.
- `point_id`: short stable local identifier for this difference.
- `aspect`: select the closest supplied enum value for the behavior that diverges.
- `positions`: the distinct complete meanings or behaviors at this point.
- `positions[].members`: bind every input representation to exactly one position.
- `source_case_ids`: copy all non-null `case_id` values from that representation's `sources`.
- `statement`: state what this position does in the shared context and what immediate result it
  produces; do not describe only a field value or SQL operator.
- `resolution`: use `conflict` for a generated difference point. The other enum values remain
  accepted for backward-compatible repair of prior analyses, but resolvable variation should be
  represented through `shared` and `residuals` rather than a new point.
- `reason`: identify the overlapping context, the first observable result difference, and why no
  supplied pre-action selector separates the positions.

Every difference point must include every input representation exactly once, including
representations that share the same position. Case IDs are provenance only, never selectors.

# Group contract

- `equivalent`: one behavior without meaningful source-relative differences. It must have
  `residuals=[]` and `residual_relations=[]`.
- `compatible`: meaningful differences remain, but they are complementary, can be parameterized,
  or have a valid selector. Any group with a residual must use `compatible`.
- A multi-member group must contain at least one `shared` statement supported by its members.
- Every `supporting_ref` in a group's `shared` and `residuals` must also appear in that same
  group's `supporting_refs`. Never cite a representation assigned to another alternative, even when
  it supports a fact shared across alternatives; state that fact separately inside each group using
  only that group's members.
- Put only meaningful source-specific behavior in `residuals`. Do not repeat an `equivalent`
  difference as a residual. A conditional residual needs an `applies_when` value available before
  applying the asset.
- Relate residuals as `compatible`, `conditional`, or `refinement`; never retain a conflict relation
  inside one group.
- Use an `equivalent` singleton for each side of an unresolved conflict. The complete source node
  already preserves that singleton's behavior, so a singleton has no residuals or residual relations.
- Representations that occupy the same position at every conflict point belong in one group. Merge
  their non-conflicting variation through `shared`/`residuals`; do not create duplicate alternatives
  that make the same conflict choices. Conversely, do not place two representations in one position
  merely because they share an operator: if their complete behaviors can still produce different
  results under the shared context and have no selector, they require distinct positions.
- Never return a group with `relation=conflicting`; represent conflict by separate groups and a
  `difference_point` with `resolution=conflict`.

# Valid selectors

A selector must come from request meaning, Schema meaning, selected tables, or stable query context.
Expression text, SQL text, plan IDs, case IDs, source IDs, representation IDs, dataset names, and
frequency are not selectors. The implementation choice itself cannot be restated as its selector.
A correlation between cases is not a reusable condition.
Before accepting a conditional position, check that its selector is true for every supporting
representation's supplied Question and observed query context. A newly invented condition that does
not hold in its source case cannot make two implementations compatible.

A selector must be independent of the behavior being selected. For every proposed selector, name
the observed pre-action fact that is true for all members of one position and not true for the other
positions. A Schema fact may explain an already selected business input, but the existence of
different fields, references, types, or constraints in the alternatives does not itself select one:
the request or stable query context must first determine which input plays that role. If the selector
can be known only after inspecting an alternative's action or references, it is circular and invalid.

Do not group positions merely because they use the same operator or similar prose. They are the same
position only when their complete behaviors remain equivalent for every valid input allowed by the
supplied evidence and Schema. When referenced inputs have different Schema meanings, preserve
separate positions unless a proven pre-action context makes the difference irrelevant.

# Output checks

- Put every input ref in exactly one group.
- Return one group when every difference is resolvable; return multiple groups only when at least one
  `difference_point` has `resolution=conflict`. When there is no conflict point, disjoint contexts
  still belong in one `compatible` group with request-visible conditional residuals.
- Preserve a historical `target_alternative_id` only on the group containing that alternative root.
- Do not invent facts, conditions, refs, or case IDs.

# Final consistency check

- Disjoint contexts or one proven selector -> `compatible`, with source-grounded residuals and no
  difference point.
- Overlapping contexts with no observable result divergence -> `equivalent`, without residuals and
  without a difference point.
- Overlapping contexts plus result-changing behaviors and no selector -> one minimal conflict point
  and separate `equivalent` groups, without singleton residuals.

# Iterative construction feedback

Read every source Question and SQL together with direct dependency definitions. First retain exact
operands and behavior, then explain differences with source evidence. Shared formulas alone do not
prove identical semantics. For conditional groups explain why each selector holds in each claimed
source, independently of the action it selects. Check overlapping scopes. Candidate construction
and coverage reviews can invalidate an earlier explanation. Revisit only affected observations and
groups. Correct candidate omissions without unnecessarily changing a sound grouping.

Return `observations` for each member: requested_result, complete action, and exact_operands.
Return `explanations` for meaningful differences: stable difference_id, involved supporting_refs,
observed_difference, explanation, applies_when, supplied evidence_ids and remaining uncertainty.
These are revisable claims. They support the proposed shared semantics/residuals and partition.
Never abstract distinct operands into the same role before establishing evidence for doing so.
