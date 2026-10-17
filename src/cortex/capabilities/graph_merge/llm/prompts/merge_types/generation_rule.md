# GenerationRule requirements for integration

A Rule owns one independently reusable SQL-generation decision, not every operation in a source
query. condition states the situation recognizable before the decision; action states the complete
behavior. Names, triggers and operation_intent help interpretation but do not determine identity or
prove applicability. It does not redefine Concept bindings or Metric formulas.

Preserve the rule's owned operands, population, row grain, NULL/DISTINCT behavior and result policy.
Different COUNT operands cannot be replaced by an unbound target ID. A ranking-only rule may consume
an established key, but cannot lose a source-owned calculation through vague delegation. Raw monthly
rows are not per-customer aggregates without evidence; preserve repeated entity rows. If raw and
grouped branches coexist, give separate SQL patterns or explicitly qualify GROUP BY as grouped-only.

Retain necessary business-to-physical mappings and references; remove a concrete example dependency
only when a meaningful generalized input replaces its role. Required/avoided behavior already owned
by a source must survive, but do not invent a contrast merely to fill a field. Facts override false
explanatory prose: RANK skips rank numbers after ties; DENSE_RANK does not. Across sources preserve
aggregation-before-composition where required, rather than introducing detail-row fan-out joins.
