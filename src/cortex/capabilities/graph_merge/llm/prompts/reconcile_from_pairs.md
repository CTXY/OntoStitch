# Role

The nodes already share one identity. Produce a `ReconcileOutcome`, not a merged node.

Your job is to preserve every supplied behavior by partitioning refs into reconciliation groups and,
when needed, conflict Difference Points.

# Evidence authority

- `input_analysis`, when supplied, is a provisional structured observation of each node's exact
  operands, complete Actions, and requested-result slots. Check it against source Questions, SQL,
  and dependencies. Revise an observation when that evidence contradicts it; do not silently
  abstract away distinct operands.
- Pair decisions are evidence, not votes. Use them to locate concrete action contrasts, then resolve
  them against the complete group and source Questions.
- `requested_results` slots identify request contexts. They do not by themselves prove that Actions
  inside a slot, or across slots, are equal.
- Do not invent missing comparisons, facts, selectors, or equivalences.

# Output schema semantics

## Groups

A group is one canonical behavior container.

- `equivalent`: every ref in the group has the same complete behavior. Equivalent groups have no
  residuals or residual relations.
- `compatible`: refs can share one canonical behavior plus source-specific residuals because every
  residual is selected by a reusable condition known before the behavior is applied, or because
  residual relations are `compatible` or `refinement`.
- Singleton groups are `equivalent` and have no residuals or residual relations.
- Every input ref appears in exactly one group.
- Use the smallest number of groups that preserves every supplied behavior.

## Residual relations

Residuals describe source-relative behavior inside one compatible group.

- Conditional residuals need `applies_when`.
- A compatible group must not contain unresolved conflict residual relations.
- Do not output `relation: conflict` inside a single compatible group.
- If you need `relation: conflict`, partition the conflict into separate groups and create a conflict
  Difference Point.

## Difference Points

A conflict Difference Point represents one unresolved choice among complete behaviors.

- With multiple resolved groups, include a grounded conflict Difference Point partitioning those
  resolved refs consistently with the groups. Explicit unresolved singletons are excluded.
- With one group, do not emit a conflict Difference Point.
- A conflict Difference Point position describes a complete outcome in the shared request, not a
  field location.
- Assign every resolved input ref to exactly one position and copy its non-null source case IDs.
- A DifferencePoint has only point_id, aspect, positions, resolution, and reason. Never add a
  top-level supporting_refs field. Representation support belongs in positions[].members, each
  containing representation_ref and source_case_ids.
- Choose `aspect` from the input node type. For a Generation Rule operation difference, use
  `rule_action`, not a Metric expression aspect.

## Unresolved evidence

If equivalence, applicability or competition cannot be established from the supplied evidence,
list those refs in `unresolved_refs`, each in an equivalent singleton group preserving the complete
source definition. Never put unresolved refs in a DifferencePoint. Multiple groups without a
conflict point are legal for unresolved singletons plus at most one resolved group. Missing evidence,
failed construction and model errors do not establish a semantic conflict.

# Decision procedure

1. Compare the requested result and selected business object before comparing Actions. Two concrete
   objects may be instances of one role. Independent requested results are not one context.
2. Name each node's exact operation and operands or references before abstracting. An Action includes
   every operation or requirement that can change returned values, selected objects, rows, row count,
   ordering, or output shape.
3. First group equivalent complete Actions. Similar wording, a common business role, or a common
   operation name is not proof of equivalent Actions.
4. To group two Actions as equivalent, show why their exact operands yield the same result for every
   valid input in the shared request.
5. To put different Actions in one compatible group, state the Question or Schema fact known before
   the Action that chooses exactly one behavior for every member, and state which refs prove it.
   An SQL observation, source ID, object name, action operand, or predicate inside the Action is not
   such a selector.
6. If result-changing Actions can apply to the same request and no proved pre-Action selector
   separates them, put those Actions in separate groups and create a conflict Difference Point.
7. Different requested-result slots, such as returning an object with the maximum value versus
   returning only the maximum numeric value, are disjoint atomic requests. Do not create a conflict
   point merely because slots differ. Use one compatible group with conditional residuals only when
   each slot truly selects the supplied behavior before the Action.

# Pair contrast rules

When supplied pair decisions say two refs have result-changing differences:

- Do not place those refs in the same group unless you can state a request-visible or schema-visible
  selector that chooses exactly one behavior before the Action.
- The selector must appear in `applies_when` or in a non-conflict residual relation.
- `requested_result` alone is not a selector for different executable Actions when the same request
  could still observe different results.
- If no selector exists, split the refs into separate groups and create a conflict Difference Point.

Pair decisions can contradict each other. A pair marked `conflict` does not override a demonstrated
difference in requested-result slots. State the group-wide resolution of any contradiction in
`reason`.

# Validation feedback

Read `working_memory` and `validation_feedback` together. For contract errors, repair the cited
output structure. For unsupported conditions, dependency misunderstandings or competition feedback,
re-examine affected observations, selectors and grouping; withdraw or revise unsupported difference
points. A checker classification asks for investigation, not an automatic conflict decision. For a
model failure carrying a validation error_chain, correct the reported fields. Preserve independently
supported semantic differences. Previously passed explanations are revisable if new evidence
contradicts them.

- A conflict Difference Point cannot coexist with one compatible group. Keep the conflicting
  positions and split the groups by their unresolved choices unless new evidence proves a
  request-visible selector.
- A compatible group cannot contain conflict residual relations. Convert the unresolved conflict to
  separate groups and a conflict Difference Point, or replace it with a supported non-conflict
  selector.

# Examples

Example: A ranks product P1 by method M; B ranks P2 by M; C ranks P3 by N. If P2 and P3 have the
same supplied request-visible property, that property cannot select M versus N. Put A and B in one
group, C in another, and describe the M/N result difference in a conflict point.

Example: A calculates shipping volume for an ordinary chair as measured volume; B does the same for
an ordinary table; C multiplies measured volume by bundle size for a sofa whose supplied Schema says
it is bundled. The selected product's bundle property selects the Action before calculation. Use one
compatible group with conditional residuals, not a conflict point.

Example: A totals the `billed_amount` field and B totals the `paid_amount` field for the same
requested total. Both use SUM and both fields are monetary amounts, but an unpaid bill makes the
results differ. Without evidence that the fields are always equal or a request-visible condition
that selects one, keep separate conflict positions.

Example: A returns the object with the highest measured value; B returns that object's requested
details; C returns only the highest numeric value. A and B can share the object-returning behavior.
C answers a different atomic request, selected by whether the user asks for an object or a number.
Put A, B, and C in one compatible group with separate conditional residuals for the two requested
results. Do not report an object-versus-number conflict.
