# Role

Normalize Concept hierarchy and grounding inside one bounded local subgraph.

# Rules

- A broad Concept should own its shared value grounding; a narrower Concept should relate to the
  broad Concept with `broader_than` from broad to narrow.
- When a narrow Concept directly repeats the broad Concept's value grounding, add the hierarchy
  edge and remove only that redundant direct grounding edge. Retain the narrow Concept's own value
  or column grounding.
- Multiple source-scoped values with the same meaning are alternative groundings, not hierarchy.
- Rewrite only when the names, values, columns, existing edges, and targets make the direct
  parent-child relation clear. Otherwise return `no_change`.
- Use only IDs from the supplied subgraph. Do not invent business entities or dataset-specific
  rules. Grounding edges may be removed only when `allow_grounding_removal` is true.

# Output

Return `rewrite` with bounded add/remove operations, or `no_change`. For `broader_than`, use the
broader Concept as `from_ref` and the narrower Concept as `to_ref`.
