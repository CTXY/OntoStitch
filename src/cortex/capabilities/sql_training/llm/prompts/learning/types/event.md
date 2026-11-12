# Event

{event_definition}

## Creation threshold

Identify the occurrence, participating objects and their roles, and its time or place together. Verify that the context describes this event rather than another object or occurrence. Merely seeing a verb, status, timestamp or JOIN is insufficient. A Concept may represent an object or status without an Event. Do not invent stages or create every possible event type.

{relation_scope_rule}

## Write the properties and relations

- Require a nonempty top-level `description` explaining what occurs and the evidenced participant/context structure.
- Optional `targets` uses the same physical matching-group format as Concept; optional `target_expressions` preserves complete supported conditions. An isolated date or non-NULL test is not automatically a complete event realization.
- When targets are empty, generate explicit relations to bound Concepts that establish the participating object and occurrence time/place. Empty targets are not permission for a name-only event.
- Use precise business predicates to distinguish roles. For example, a sender and recipient should not become two unexplained has_participant edges. Do not add role or qualifier fields.

{event_binding_rule}

## Complete node example

Illustration only: case evidence identifies order placement, an Order object and its Placement date attribute. Both Concepts must exist with valid bindings before or within the same edit. The Event describes their occurrence structure, without asserting one database row per event.

```json
{
  "id": "event.order_placement",
  "node_type": "event",
  "name": "order placement",
  "description": "The occurrence in which an order is placed; Order is the placed object and Placement date records this occurrence's date.",
  "properties": {"targets": []}
}
```

## Explicit edges example

The names are illustrative business predicates, not mandatory labels. Use only meanings supported in the actual case.

```json
[
  {
    "operation": "create_edge",
    "edge": {
      "id": "edge.order_placement_places_order",
      "edge_type": "places_order",
      "from_node_id": "event.order_placement",
      "to_node_id": "concept.order",
      "properties": {"description": "Order is the business object placed by this occurrence; this does not assert that every source row is one distinct placement event."}
    }
  },
  {
    "operation": "create_edge",
    "edge": {
      "id": "edge.order_placement_occurrence_date",
      "edge_type": "occurs_on_date",
      "from_node_id": "event.order_placement",
      "to_node_id": "concept.placement_date",
      "properties": {"description": "Placement date records when the order was placed; missing recorded dates do not by themselves prove that placement never occurred."}
    }
  }
]
```

Composition does not imply sequence or causality. Sharing a table does not prove participation. Grounding or a recorded date does not prove countability, completion, complete history or exhaustive stages.
