# Concept

Apply the shared business-referent and NL minimality rules. A name/label is not an identifier without evidence. Keep profile counts in investigation notes, not permanent definitions.

## Write the properties

- targets contains nonempty joint Column/Value matching groups. No Table targets. Identifier/name representations can be separate outer groups; explain their roles without assuming interchangeability. Repeating a Value's owner Column does not form a joint match.
- Use target_expressions with zero-based group index keys only for a necessary executable condition/correlation. Use catalog SQL names, not graph IDs or undeclared aliases.
- Reuse verified Values; propose missing ones via edit_graph.values and use returned IDs.
- Author grounded_on explicitly where this Concept owns the direct binding. Targets do not create edges.

### Illustration: School

If evidence supports these roles, School.targets = [[column.schools.CDSCode], [column.schools.School]]. These represent one School through identifier/name roles, not a joint key or interchangeable count operands. Do not rename it School name merely because SQL uses the name field. This is an illustration, not a domain-specific rule.

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

