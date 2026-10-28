# Generation Rule

{rule_atomicity}

Extract a Rule only when the case supports an independently useful decision beyond restating its query. Direct extracts supported requested operations, not ordinary joins or speculative corrections. Repair may learn a correction from an observed error. Reuse supplied physical join facts.

The guide response includes `direct_rule_registry`; no separate registry call is required. For Direct, choose `operation_intent` from that registry. Request taxonomy only when you need additional operation examples.

## Write the properties

- `condition`: the smallest business request requiring the action, before choosing an implementation.
- `action`: the required operation and necessary physical bindings. Use a local SQL pattern with explained role placeholders only if needed to remove ambiguity.
- `reason`: the observable difference from a plausible alternative, supported by evidence or SQL semantics. Do not assume data cardinality or claim an entire SQL construct is incorrect.
- `references`: direct, jointly required dependencies of the condition/action. Prefer the complete Concept/Metric being operated on. Add a physical reference only when the Rule itself owns a separate physical choice; do not expand every dependency to its Tables and Columns. Omit a reference if the condition/action remain complete without it. Runtime argument values do not become fixed dependencies. Retrieval hints are not dependencies.
- `triggers.question.phrases`: predictive contiguous phrases copied from the Question (only case and whitespace may differ). Each is an OR retrieval signal, not proof of applicability.
- An observed error may justify a corrective Rule alongside the Concept or Metric it uses. Record the supported wrong alternative and correct behavior without replacing their definitions.
- Direct chooses `operation_intent` from the supplied registry and leaves both SQL trigger lists empty. Repair may use one primary behavior kind and evidenced predictive SQL error signals. SQL trigger columns do not create dependency edges.
- Keep current K, thresholds and dates as runtime inputs; do not bake them or query aliases into the rule. A count Rule can refer to the entity Table without inventing a primary-key Concept.

## Complete node example

Illustration only: suppose the Question asks “How many orders?” and Gold uses `COUNT(*) FROM orders`. Pass this complete node in `edit_graph.upserts`, replacing example identifiers with actual evidence.

```json
{
  "id": "generation_rule.count_orders",
  "node_type": "generation_rule",
  "name": "count orders",
  "properties": {
    "operation_intent": "count_aggregation",
    "condition": "The request asks how many orders are in the source.",
    "action": "Count rows of orders with COUNT(*).",
    "reason": "COUNT(*) returns the number of orders, including rows whose optional fields are NULL; counting an optional column can omit them.",
    "references": ["table.orders"],
    "triggers": {"question": {"phrases": ["How many orders"]}, "sql": {"columns": [], "operations": []}}
  }
}
```

## Refs-generated relations

Rule `references` to Concept/Metric generate `applies_to`; references to Table/Column/Value generate `grounded_on`. SQL trigger Columns are retrieval signals and do not generate dependency edges. Independent relations are written explicitly, not by adding unrelated endpoints to references.
