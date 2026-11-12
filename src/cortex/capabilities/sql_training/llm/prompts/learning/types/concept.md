# Concept

{concept_definition}

## Minimal meaning

Test the NL meaning for two or more complete Concept/Metric/NL-term units with distinct field/value/operation matching roles that reconstruct the whole meaning. Give those units, matches and composition when rejecting. Units need not exist as nodes. Joint fields for one term, shared targets and definition expansion alone do not prove a split. Do not bundle a filtered request or split by words. Choose a useful representation from case meaning, metadata and data evidence without a fixed state/attribute/category decision tree.

Keep dataset-profile row counts and current filter results in investigation notes, not permanent business definitions. Preserve relevant qualitative limits. A name/label field is not an identifier without evidence of identity semantics.

## Write the properties

- Require nonempty `targets`, with nonempty lists of exact Column or governed DimensionValue IDs. `[[A, B], [C]]` means two matching groups; A and B jointly form the first.
- Use optional `target_expressions` with zero-based index keys such as `"0"` when a matching group requires a complete executable condition or correlation. Use actual catalog SQL names, not graph IDs or undeclared query aliases. Do not invent a predicate merely to fill this field.
- No Table targets or Concept-to-Table grounded_on. Table grain/coverage belong in metadata. Outer items may realize the same object through identifier/name roles; explain their uses. Inner members must be jointly necessary; a governed Value already identifies its Column, so do not repeat that Column merely to express equality. Do not assume role-specific matches are interchangeable or that a name is a unique identifier.
- Targets may overlap across Concepts. Direct grounded_on connections are unique per physical endpoint across active Concepts; one Concept can connect to several endpoints. Targets are not an automatic out-edge list.
- Reuse existing verified Values. Propose missing Values through edit_graph.values, then use the returned canonical IDs; do not guess Value IDs or write raw physical nodes.

{concept_matching_example}

## Complete node example

Illustration: stored “order amount” in orders.amount. Replace with case evidence; use edit_graph.upserts.

```json
{
  "id": "concept.order_amount",
  "node_type": "concept",
  "name": "order amount",
  "description": "The recorded monetary amount of an order; a stored input rather than a derived calculation.",
  "properties": {"targets": [["column.orders.amount"]]}
}
```

Author grounded_on explicitly when this Concept owns the direct binding. Targets alone do not create edges. The relation guide specifies the edge operation shape.

Business relations between Concepts must express supported meanings. A shared target alone does not prove equivalence, hierarchy or overlap of actual records. Keep related nodes and their explicit relations in one coherent edit.

Optional relation_metadata: group existing edges; inspect both directions and read the relation guide.
