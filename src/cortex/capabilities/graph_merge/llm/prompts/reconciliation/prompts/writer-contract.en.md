# Editing and Submission Specification

## 1. Incremental Draft Editing
- Maintain one draft relative to the fixed before graph. Each call edits that draft; it does not replace the whole draft or execute on the previous candidate.
- Always send `edit_draft({"remove_changes": [...], "put_changes": [...]})` without wrappers. Omitted draft entries remain unchanged.
- To revise an entry, withdraw its change_id and add the replacement with a NEW change_id in the SAME call. Both actions are atomic. Do not overwrite an existing change_id with different content.
- Example: replace a proposed deletion c1 with a relation replacement: `{"remove_changes":["c1"],"put_changes":[{"change_id":"c2","kind":"edge_replace","edge_id":"E1","replacement_edge_id":"E2","reason":"Retain the assertion and migrate its evidence to E2."}]}`.
- Withdrawing a draft entry does not delete a graph object. Graph deletion needs an explicit graph operation.
- Two empty arrays keep the current draft and request independent assessment; they do NOT clear it. To clear the draft, withdraw all current change_ids.
- Format/handle/operation-combination errors reject the entire edit before saving. Otherwise the draft is saved and compiled. Compilation failure retains the draft and returns specific feedback. Compilation success automatically enters independent Assessment.
- A replay of the same edit does not apply twice or increase the version. Versions and checkpoint bindings are controller-managed.

## 2. Graph Operations Overview

| `kind` | Required business fields and meaning |
| --- | --- |
| `node_create` | `node_id / node_type / content / evidence_ids`; complete business content; for type fields see "Node field requirements". |
| `node_update` | `node_id / set`; modify an ordinary node. |
| `edge_create` | `edge_id / edge_type / from_node_id / to_node_id / properties / evidence_ids`; `properties.description` is mandatory for every new edge, even when the predicate has no extra fields; it explains the real business relationship. |
| `edge_update` | `edge_id / set`; modify predicate, endpoints, or properties; retain original sources. |
| `edge_replace` | `edge_id / replacement_edge_id`; the knowledge of the old edge is carried by another real live edge; Controller merges the old edge's sources into the successor before deleting the old edge. |
| `edge_remove` | `edge_id / refs`; revoke only assertions that are truly unsupported or erroneous, citing specific `before` basis; use `edge_replace` when a successor should be retained. |
| `relation_declare` | `contract`; define a concrete business predicate not yet installed. |

## 3. Field, Replacement, and Reference Constraints
- **Write boundary**: Existing object updates, removals, and replacement successors must remain within the listed write scope. A visible endpoint is not automatically editable. New objects must directly address this round's issue and use authorized endpoints and evidence.
- **Update rules**: Updates are relative to fixed `before`: omitted fields are retained, `set.properties` replaces by key; deleting optional properties requires explicit `unset:["properties.<key>"]`. `null` does not mean deletion. Add sources with public `evidence_ids`; original sources are retained by code.
- **Edge replacement rules**: The replacement edge and successor must be different; the successor may be newly created in the same batch but must ultimately survive; replacement chains are prohibited. One additional `edge_update` may be used to consolidate the successor `description`. Saying "already retained" in the explanation cannot replace a real successor and source migration.
- **Explanation and contracts**: `reason` explains this modification; `description` describes long-term business facts, not repair logs. Use only provided predicate contracts, or explicitly declare a new contract. When an existing contract for other endpoint types is truly needed, first use the currently exposed `read_predicates` to read; not being shown does not mean not installed.
- **`refs` format**: use `{view:"before",kind:"node|edge|contract",id:"real ID",field:"public field path (optional)"}`; for example `properties.definition` or `evidence_ids`. Do not reference storage-layer source fields.

## 4. Per-round Reply Discipline and Fallback
- Choose one: batch read or one `edit_draft`. Do not mix, do not restate the entire graph.
- Assessment feedback states the problem and the results that must be retained; equivalent repair is allowed.
- When truly unable to complete, output `{problem,refs}` explaining the specific reason, retain the incomplete checkpoint, and do not pretend success.
