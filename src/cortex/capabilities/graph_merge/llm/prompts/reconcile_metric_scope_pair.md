# Task

The two nodes are candidate Metrics for one identity. Decide whether an apparent scope or grouping
difference is intrinsic to the Metric or only a current-case usage detail. Return one pair decision.

# Metric contract for this decision

- A Metric's intrinsic calculation must be explicit in its expression and references. Explicit
  predicates, required calculation grain, source alignment, or NULL/zero behavior may be intrinsic.
- Current Question filters and grouping fields may appear in a Metric description or SQL plan to
  explain the proved case. They are not fixed Metric behavior unless the expression, references, or
  other node contract explicitly requires them.
- A caller may group a Metric by a requested dimension. Different illustrative groupings do not
  change a Metric's result for the same selected records and requested grouping.
- Do not infer a mandatory scalar or per-dimension output shape from a SQL plan example.
- Different formulas, operands, explicit fixed predicates, required grain, source alignment, or
  NULL/zero policies are Action differences when they can change the result.

# Decision rules

1. State each intrinsic calculation from expression, references, and explicitly required behavior.
2. Compare the two calculations under the same selected records and caller-requested grouping.
3. Use `equivalent` only when the final metric value is the same for every valid input.
4. Use `conflict` only when a result-changing difference is intrinsic and no proved pre-calculation
   selector chooses between them. A source case, SQL plan, operand, or current grouping is not a
   selector.
5. Copy refs exactly. `equivalent` requires no selector; `conflict` requires different Actions and
   no selector.
