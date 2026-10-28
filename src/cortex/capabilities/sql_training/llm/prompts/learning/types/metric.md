# Metric

{metric_atomicity}

Keep a stored meaning in Concept. Preserve all casts, units, scaling, operation order and intrinsic population required by the Metric's own formula; leave separately requested aggregation, filtering, ranking and presentation to the query operation.

## Write the properties

- `expression.expr`: the complete defining formula in the SQL dialect, using `${key}` variables. `expression.refs` binds every key to an exact physical or Metric ID, with no unused keys.
- `references`: exactly the dependencies used by those bindings. Do not use Concept IDs as formula operands or add transitive Table/Column references to expand retrieval.
- `pattern`: a non-empty statement of that same calculation.
- `sql_generation_plans`: at least one plan with local `id`, `application` and ordered `steps` implementing only that formula. Physical tokens use `<table.column>`, not ontology IDs. Do not turn the plan into the current case's whole query.

## Complete node example

Illustration only: suppose the Question names “order amount variance” and Gold computes its population-variance formula below. The operations jointly define variance; they are not a request to average an already complete variance. Replace example identifiers with actual evidence. Pass the complete node in `edit_graph.upserts`.

```json
{
  "id": "metric.order_amount_variance",
  "node_type": "metric",
  "name": "order amount variance",
  "properties": {
    "expression": {"expr": "AVG(${amount} * ${amount}) - AVG(${amount}) * AVG(${amount})", "refs": {"amount": "column.orders.amount"}},
    "references": ["column.orders.amount"],
    "pattern": "Mean squared amount minus the square of mean amount.",
    "sql_generation_plans": [{"id": "population_variance", "application": "Calculate order amount variance.", "steps": ["Compute AVG(<orders.amount> * <orders.amount>).", "Subtract AVG(<orders.amount>) * AVG(<orders.amount>)."]}]
  }
}
```

## Refs-generated relations

Metric expression dependencies on Metric generate `depends_on`; physical dependencies generate `grounded_on`. Plan-only tokens do not add formula dependency edges. Independent relations are written explicitly; do not change the formula merely to obtain an edge.
