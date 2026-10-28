# Concept

{concept_definition}

## Write the properties

- `targets`: one or more complete physical bindings of the same meaning. Each has a stable local `id`, a qualified `sql_expression`, and the exact Column/Value IDs used in `references`. A complete expression may involve several tables; preserve its required association and correlation conditions instead of splitting it into incomplete targets. Use actual catalog `table.column` SQL names, quoted only when needed; scoped aliases are allowed when declared by the expression, not unexplained aliases from a source query. Do not use graph IDs or `<table.column>` plan tokens as SQL identifiers. A field uses its Column and a category its complete expression. Tables are not Concept target references.
- Keep independently named meanings separate. Add alternate targets only when evidenced by this case. `mapping_type` is optional classification.
- Multiple targets do not by themselves mean equivalent or interchangeable implementations. Do not flatten ambiguous external-context choices into alternate targets. Do not add fields to Concept or target.
- A `dimension_value` is a governed stored value owned by a Column, not an arbitrary JSON value or a current threshold. For a stable category, submit its complete Concept predicate and Column reference; code verifies the database value and adds the Value node/reference. For independent Value creation, read the dimension_value guide and submit column_id/value in edit_graph.values. Reuse the returned Value ID; do not guess it or submit raw physical nodes.

## Complete node example

Illustration only: suppose the Question names “order amount” and Gold reads `orders.amount`. Replace this example's names and IDs with actual case evidence. Pass this node in `edit_graph.upserts`.

```json
{
  "id": "concept.order_amount",
  "node_type": "concept",
  "name": "order amount",
  "properties": {
    "targets": [{"id": "amount", "sql_expression": "orders.amount", "references": ["column.orders.amount"]}]
  }
}
```

## Refs-generated relations

Each target reference generates `grounded_on`: Concept → Column/Value, with that target's local `id` in edge `properties.target_id`. This preserves which binding uses the physical fact. Independent relations, such as an evidenced Concept hierarchy, are written explicitly without changing a target's grounding references.
