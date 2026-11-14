# Metric

Apply shared NL minimality and calculation admission. Stored inputs are Concepts; preserve defining coupled operations, not independent request operators.

## Write the properties

- expression.expr contains the complete calculation with ${key} operands; expression.refs maps every key to an exact supported ID, with no unused keys. An object/label does not become a precomputed count through its placeholder name.
- Author depends_on for direct Concept/Metric operands and grounded_on for physical inputs. No general references field; indirect paths do not replace operands.
- pattern and optional sql_generation_plans implement the same calculation. Plans use <table.column>, not graph IDs, and cannot hide missing expression operations.
- Preserve supported population, units and grain. COUNT(nullable_field), COUNT(*), conditional counts and DISTINCT are not interchangeable from current result equality. Do not invent NULL, imputation or zero-denominator policies.

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
