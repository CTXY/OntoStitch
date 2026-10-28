# Governed Values

A Value is one actual stored scalar owned by an existing Column. It represents a reusable named category or entity, not a current threshold, K, report date or arbitrary JSON property value.

## Look up and reuse first

1. Identify the owner Column from the supplied bindings or graph evidence. If an exact Value ID, owner and typed stored value are already known, reuse it without repeating the lookup.
2. Otherwise read that Column with `inspect_graph(mode="node", node_id=...)`. Its response includes incoming and outgoing relations. Locate its Value candidates, then read those nodes to compare their owner Column, stored scalar and scalar type. Matching names alone are insufficient; absence from the case overview does not prove absence from the graph.
3. Reuse the matching Value ID. If a lookup result is incomplete, inspect the remaining relevant result before concluding that a Value is missing.
4. Only when no matching Value exists, submit a proposal through `edit_graph.values`. Investigate the database first only if the stored type, spelling or encoding is uncertain; the write service performs its required verification either way.

## Add a missing Value

Copy the exact Column ID and typed stored value. Code verifies the database, allocates the canonical ID and adds `value_of`: Value → owner Column. Reuse the returned `value_ids`; do not guess IDs. Tables and Columns remain managed physical facts.

Illustration only, if the case supports `North` in `orders.region` and no matching Value exists:

```json
{"column_id":"column.orders.region","value":"North"}
```

This is one item in the edit's `values` list. An unsupported value rejects the whole edit. A Concept's complete category predicate can also trigger the same verification and materialization automatically; both paths share Value identity.
