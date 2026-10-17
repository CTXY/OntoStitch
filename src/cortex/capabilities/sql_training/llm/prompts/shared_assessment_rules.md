# Shared Assessment Rules

1. Read deterministic retrieval and selection facts before judging application.
2. Judge application and intrinsic asset quality independently.
3. SQL that adds, removes, or changes a Candidate-owned operation is an application failure unless
   the Candidate requires that change; it is not by itself an intrinsic defect.
4. Stable SQL does not prove an asset valid, and failure to apply an asset does not prove it invalid.
5. Report a missing contract only when it is absent from expression, pattern, and every plan step.
6. Require semantic equivalence, not a particular CASE, CTE, join, or subquery shape.
7. Every quality defect identifies an actual member of the current Candidate set and its field.
8. A regression requires direct attribution.
9. MIN/MAX over a directly stored Concept selects an existing value. If the candidate-owned
   binding/formula is applied, a wrong external selector or grouping does not change its usage or
   quality; it is a Generation Rule application failure unless the Candidate prescribes it.
10. Assess a Metric plan step by step and in order. Mentioning the same tables, columns, formula,
    or final value is not application evidence. If a plan requires source-local aggregation before
    a join, SQL that joins detail rows first and aggregates afterward is `partially_applied` or
    `not_used`, even when its final SELECT text resembles the Metric expression.
11. Judge only Metric-owned steps. Differences in an external filter, grouping, ranking, or output
    do not make the Metric unused when its complete formula and required input handling are applied.
12. For a Generation Rule, condition, action, and reason describe one evidence-supported decision
    contract. Target resolution proves application effect, not reason factuality. Directional
    claims about rows, cardinality, aggregate effects, ordering, ties, values, or NULL behavior
    must be supported by the frozen evidence and must describe the correct side of the contrast.
13. Generalization preserves each proved business input and its use. A Candidate is invalid when
    it replaces one evidenced role with an arbitrary dimension, field, source, or operand without
    evidence for that broader applicability. Preserving a proved business role does not make a
    current-request filtering or grouping field intrinsic to every use of a Metric.
14. A reason stops at the action's first result. Do not attribute a later consumer's NULL-to-zero,
    aggregate, formula, or presentation effect to an earlier join, filter, or grouping action.
15. Requesting a named Metric does not independently request each internal step in its plan. A Rule
    needs a separately matched request or result operation over the completed Metric or meanings.
16. Judge Metric quality from its atomic Question term and expression. The expression must be the
    complete formula for that term, use exactly its necessary dependencies, and exclude behavior
    separately requested by the Question. A generation plan fails quality only when it copies a
    concrete value supplied solely for the current request. Its level of detail and value-free
    request roles are not intrinsic quality defects.

Application failure normally requires asset-content learning. Initial not_used Candidates do not
enter Working. Residual not_selected, not_applied, or partially_applied Candidates use content
refinement except for the residual prompt's narrow `application_policy` case: a valid, retrieved,
selected Concept or Metric lost to a proved non-equivalent alternative implementation.
