# Local submission contract

Return one `BatchResult` with the required patch and checks; valid submission finishes the task. Updates contain the ID and changed fields; creates require complete objects. Do not repeat accumulated history or use StageCompletion.

## Shapes

Preserve accepted Rule conditions, actions, reasons and complete alternatives. Judge whether integration preserves each original decision; do not re-audit source factual claims or atomicity. Compare necessary consumers only to prevent changes from mixing incompatible branches.

Only return inspection_checks for IDs in inspection_signals. No per-Rule rationale audit is requested. Do not invent extra inspection IDs. With no saved_patch and pending_revision zero there is no pending data to inspect; the pending tool appears after data is saved.

- Node edits: `create_node` with complete `node`, or `update_node` with `node: {id, ...changed fields}`; deletes use only `node_id`. Required properties come from the preloaded type guidance.
- Create fresh node/edge IDs under the supplied `new_id_prefix`, with a local descriptive suffix. Never guess the next `input::n...` or `input::e...`: unread IDs may already belong to physical catalogue objects. Keep a pending new object's ID when repairing it.
- Edge edits: `create_edge` with complete `edge`, or `update_edge` with `edge: {id, ...changed fields}`; deletes use only `edge_id`. An edge has `id`, `edge_type`, `from_node_id`, `to_node_id`, `properties`, optional `sources` and `reference_origin`. Put branch scope in `properties.alternative_ids`, never a top-level alternative field.
- Introduce or revise alternatives through `conflicts`: owner `node_id`, complete `alternatives` (alternative_id, content, evidence_ids), and conflict_points. Code performs lossless compaction. Do not hand-write overrides or use set_conflict edits. Ordinary competing copies do not preserve a conflict.
- Rule triggers have separate `question: {phrases: [...]}` and `sql: {columns: [...], operations: [...]}` objects.
- A new predicate needs id, name, business description, endpoint types, direction_semantics, and evidence_requirement (what supports its assertion). Reuse an installed predicate by edge_type without resending its definition; do not restyle its name/description as a new contract. Conflicting installed definitions are rejected and not saved. When extending has_attribute, inherit property fields unchanged; do not redeclare description properties. Specific business meaning belongs in the contract and edge descriptions.

## Repair

`edits` execute graph modifications; `changes` ONLY records provenance correspondence. To remove a current copy, submit `delete_node` with `node_id` and delete/retarget its incident edges, PLUS the corresponding `changes`. Writing “delete/replaced by” in a correspondence does not remove anything. Resolve every Assessment issue on the actual candidate, including necessary related objects now made writable; these are part of this repair, not unrelated cleanup.

In correspondence references, `alternative_id` identifies a node branch only. Reference an edge by kind/id; its branch applicability belongs in the edge's `properties.alternative_ids`.

Pending objects remain until replaced or withdrawn. Use `remove_edits` (kind/id), `remove_edge_merges` (survivor IDs), `remove_contracts` (new pending predicate IDs), or `remove_changes` (complete old before-group). Omitting an operation does not remove it. Never list a before reference twice in overlapping changes/edge_merges.

Use `edge_merges` only after confirming the same assertion and compatible scope. Prefer it to copying whole edges when only exact provenance needs consolidation. Code preserves exact references; it does not decide semantic equivalence. Source-union membership is not proof that each source supports every output.

The current candidate already contains the validated saved edits. `saved_patch` is only their ID/operation directory and explicit dispositions, not another graph copy. Read an unprovided current object by ID when needed; replace only affected operations or complete overlapping disposition groups.

Use inspect_pending for invalid saved objects or dispositions not visible in candidate; never infer that a missing candidate object means its pending edit was lost.

Ordinary updates retain exact existing sources in code. Complete conflict alternatives use evidence_ids, or reuse an existing alternative_id to retain its precise sources. Do not reconstruct internal SourceReference dictionaries. conflicts requires multiple alternatives; use resolutions for an explicit evidenced selection of one existing alternative, explaining every discarded alternative. Code performs branch projection and provenance bookkeeping; do not resubmit those automatic edits.

Input record evidence_ids describe attribution, not GraphNode/GraphEdge fields. For a new ordinary node/edge or additional support, put evidence_ids beside node/edge in the edit (e.g. {operation:create_node,node:{...},evidence_ids:[...]}). Omit unchanged internal sources; code retains existing exact references. Do not copy evidence_ids inside node/edge.

Updates retain unmentioned top-level fields and property keys. Each supplied property value (including expression, targets and relation_metadata) replaces that whole value; provide it completely. Code validates the reconstructed complete object and retains exact sources.
Set a property key to null to remove that property explicitly (e.g. alternative_ids:null clears a branch scope); omitting the key preserves it. The reconstructed result must still satisfy the node/edge contract.
