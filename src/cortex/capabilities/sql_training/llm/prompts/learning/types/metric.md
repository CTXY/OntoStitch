# Metric

{metric_atomicity}

Use Concept for named stored inputs. Keep intrinsic formula details; separate request operations.

{count_semantics_rule}

## Split test

Apply the shared NL split test to name, definition and implementation. Renaming or hiding request operations in plans does not fix metric_minimality.

## Write the properties

- `expression.expr` is the complete calculation using `${key}` placeholders. `expression.refs` maps every key to an exact Concept, Metric or supported physical ID, with no unused keys.
- There is no general `references` field. Write depends_on for direct semantic operands and grounded_on for direct physical inputs through edge_operations. An indirect path cannot replace a required formula edge. Repeated uses of one operand share one edge; formula positions and description explain its roles. A prose plan does not create dependencies.
- `pattern` explains the same formula. Optional `sql_generation_plans` have local id/application/steps implementing only that calculation. Physical plan tokens use `<table.column>`, not graph IDs.
- For ratios, explain numerator, denominator, unit and compatible grain. Do not invent zero-denominator or NULL handling absent from the evidence.

## Complete node example

Illustration only: suppose evidence defines profit margin percentage as net profit divided by revenue, multiplied by 100. The two stored quantities already exist as bound Concepts at the same population and record grain. Replace all example IDs and terms with case evidence.

```json
{
  "id": "metric.profit_margin_percentage",
  "node_type": "metric",
  "name": "profit margin percentage",
  "description": "Percentage of revenue represented by net profit, using numerator and denominator at the same grain.",
  "properties": {
    "expression": {
      "expr": "100.0 * ${net_profit} / ${revenue}",
      "refs": {
        "net_profit": "concept.net_profit",
        "revenue": "concept.revenue"
      }
    },
    "pattern": "Net profit divided by revenue, multiplied by 100.",
    "sql_generation_plans": [{
      "id": "same_grain_ratio",
      "application": "Numerator and denominator are available at the same supported population and record grain.",
      "steps": ["Divide net profit by revenue and multiply by 100, preserving supported numeric and NULL behavior."]
    }]
  }
}
```

## Explicit edges example

Create the two Concept operands and their physical bindings first or in the same edit. Include these operations in edit_graph.edge_operations.

```json
[
  {
    "operation": "create_edge",
    "edge": {
      "id": "edge.profit_margin_percentage_numerator",
      "edge_type": "depends_on",
      "from_node_id": "metric.profit_margin_percentage",
      "to_node_id": "concept.net_profit",
      "properties": {"description": "Net profit is the numerator; its accounting scope and record grain must match Revenue."}
    }
  },
  {
    "operation": "create_edge",
    "edge": {
      "id": "edge.profit_margin_percentage_denominator",
      "edge_type": "depends_on",
      "from_node_id": "metric.profit_margin_percentage",
      "to_node_id": "concept.revenue",
      "properties": {"description": "Revenue is the denominator for the same population and record grain; missing-value and zero-denominator behavior must follow the supported formula."}
    }
  }
]
```

A deterministic missing-edge check may recognize formula references, but the Agent should generate the complete explicit relationships. Do not change a formula merely to obtain a business edge.
