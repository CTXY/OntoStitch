# Write an explicit relation

Use `edit_graph.edge_operations` for an evidenced connection not adequately expressed by the default relation. Endpoints must already exist or be created in the same edit. Existing relation names retain their schema constraints; request `inspect_graph(mode="relations", edge_type=...)` for the chosen relation's fields. Custom nonempty names are supported without registration, but their meaning still needs evidence.

Refs are write-time anchors: code generates a default relation for each connection. An explicit edge on that same source–target anchor replaces its default relation; it does not duplicate it. A Concept anchor also has a local `properties.target_id`; supply it when multiple targets use the same endpoint. Independent explicit edges do not add formula or binding refs. Code maintains `reference_origin` and branch metadata; never submit them.

## Edge operation example

Illustration only: after the case has established both Concept endpoints and their broader/narrower meaning:

```json
{"operation":"create_edge","edge":{"id":"edge.category_hierarchy","edge_type":"broader_than","from_node_id":"concept.category","to_node_id":"concept.subcategory","properties":{}}}
```

Use `update_edge` with the full edge to replace it, or `delete_edge` with `edge_id` to delete it. Deleting an override restores the default if its refs anchor remains. Revert tentative edges with `revert_ids`. Schema, endpoint, duplicate and physical-owner conflicts reject the complete edit; no partial write occurs. SQL stability and Assessment still govern acceptance of nodes, Values and edges.
