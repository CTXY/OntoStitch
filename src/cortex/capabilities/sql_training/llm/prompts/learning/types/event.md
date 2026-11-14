# Event

Apply shared Event admission: occurrence, participant roles, and related occurrence time or place jointly supported. Do not infer an event solely from a timestamp/status/JOIN.

## Write the properties and relations

Describe the actual structure; do not invent workflow, stages, instance grain or historical location from a participant's current location. Use role-specific business predicates, not an ambiguous participant label.

Optional targets must represent supported occurrence matching groups, not an isolated timestamp/identifier or a non-NULL test. An Event may instead be anchored through explicitly related bound participant and time/place Concepts. Empty targets do not justify an empty-shell Event.

Composition does not imply sequence or causality. Grounding/date presence does not prove countability, completion or complete history. Use actual catalog IDs and author edges explicitly.

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
