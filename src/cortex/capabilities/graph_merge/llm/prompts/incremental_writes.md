# Local submission contract

Return one `BatchResult` with the required patch and checks; valid submission finishes the task. Current-relative writes are complete objects. Do not repeat accumulated history or use StageCompletion.

## Shapes

- Node edits: `create_node` or `update_node` with `node`; deletes use only `node_id`. Required properties come from the preloaded type guidance.
- Edge edits: `create_edge` or `update_edge` with `edge`; deletes use only `edge_id`. An edge has `id`, `edge_type`, `from_node_id`, `to_node_id`, `properties`, optional `sources` and `reference_origin`. Put branch scope in `properties.alternative_ids`, never a top-level alternative field.
- Introduce or revise alternatives through `conflicts`: owner `node_id`, complete `alternatives` (alternative_id, content, actual sources), and conflict_points. Code performs lossless compaction. Do not hand-write overrides or use set_conflict edits. Ordinary competing copies do not preserve a conflict.
- Rule triggers have separate `question: {phrases: [...]}` and `sql: {columns: [...], operations: [...]}` objects.
- A new predicate needs a concise business contract. When extending has_attribute, inherit property fields unchanged; do not redeclare description properties. Specific business meaning belongs in the contract and edge descriptions.

## Repair

Pending objects remain until replaced or withdrawn. Use `remove_edits` (kind/id), `remove_edge_merges` (survivor IDs), or `remove_changes` (complete old before-group). Omitting an operation does not remove it. Never list a before reference twice in overlapping changes/edge_merges.

Use `edge_merges` only after confirming the same assertion and compatible scope. Prefer it to copying whole edges when only exact provenance needs consolidation. Code preserves exact references; it does not decide semantic equivalence. Source-union membership is not proof that each source supports every output.
