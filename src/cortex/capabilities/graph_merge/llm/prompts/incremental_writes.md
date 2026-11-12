# Local submission contract

Return one `BatchResult` with the required patch and checks; valid submission finishes the task. Current-relative writes are complete objects. Do not repeat accumulated history or use StageCompletion.

## Shapes

Before writing a Rule, identify its independently variable decision. Record selection, field selection and arrangement of already-selected content are independent when changing one does not require changing the others; split such actions and preserve their individual sources. ORDER BY plus LIMIT can implement one ranking decision: SQL clause count is not the atomicity test. Use the operation registry to describe the decision, not to justify a compound action. Compare every ordinary Rule in `decision_neighborhoods` with complete conflict branches; do not leave an unscoped duplicate of an alternative. Existing source text and Gold agreement alone do not prove sound organization or factual reasons.

Only return inspection_checks for IDs in inspection_signals, and rationale_checks for requested or actually changed Rules. Do not invent extra inspection IDs. With no saved_patch and pending_revision zero there is no pending data to inspect; the pending tool appears after data is saved.

- Node edits: `create_node` or `update_node` with `node`; deletes use only `node_id`. Required properties come from the preloaded type guidance.
- Create fresh node/edge IDs under the supplied `new_id_prefix`, with a local descriptive suffix. Never guess the next `input::n...` or `input::e...`: unread IDs may already belong to physical catalogue objects. Keep a pending new object's ID when repairing it.
- Edge edits: `create_edge` or `update_edge` with `edge`; deletes use only `edge_id`. An edge has `id`, `edge_type`, `from_node_id`, `to_node_id`, `properties`, optional `sources` and `reference_origin`. Put branch scope in `properties.alternative_ids`, never a top-level alternative field.
- Introduce or revise alternatives through `conflicts`: owner `node_id`, complete `alternatives` (alternative_id, content, actual sources), and conflict_points. Code performs lossless compaction. Do not hand-write overrides or use set_conflict edits. Ordinary competing copies do not preserve a conflict.
- Rule triggers have separate `question: {phrases: [...]}` and `sql: {columns: [...], operations: [...]}` objects.
- A new predicate needs id, name, business description, endpoint types, direction_semantics, and evidence_requirement (what supports its assertion). Reuse an installed predicate by edge_type without resending its definition; do not restyle its name/description as a new contract. Conflicting installed definitions are rejected and not saved. When extending has_attribute, inherit property fields unchanged; do not redeclare description properties. Specific business meaning belongs in the contract and edge descriptions.

## Repair

`edits` execute graph modifications; `changes` ONLY records provenance correspondence. To remove a current copy, submit `delete_node` with `node_id` and delete/retarget its incident edges, PLUS the corresponding `changes`. Writing “delete/replaced by” in a correspondence does not remove anything. Resolve every Assessment issue on the actual candidate, including necessary related objects now made writable; these are part of this repair, not unrelated cleanup.

Pending objects remain until replaced or withdrawn. Use `remove_edits` (kind/id), `remove_edge_merges` (survivor IDs), `remove_contracts` (new pending predicate IDs), or `remove_changes` (complete old before-group). Omitting an operation does not remove it. Never list a before reference twice in overlapping changes/edge_merges.

Use `edge_merges` only after confirming the same assertion and compatible scope. Prefer it to copying whole edges when only exact provenance needs consolidation. Code preserves exact references; it does not decide semantic equivalence. Source-union membership is not proof that each source supports every output.

The current candidate already contains the validated saved edits. `saved_patch` is only their ID/operation directory and explicit dispositions, not another graph copy. Read an unprovided current object by ID when needed; replace only affected operations or complete overlapping disposition groups.

Use inspect_pending for invalid saved objects or dispositions not visible in candidate; never infer that a missing candidate object means its pending edit was lost.
