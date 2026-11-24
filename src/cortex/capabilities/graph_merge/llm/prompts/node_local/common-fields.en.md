# Common Fields and Integration Principles

This module is the semantic standard shared by **Writer** and **Assessment**; specific responsibilities and outputs are governed by the following role module. The rules apply to the normal result and each complete `alternative`.

**Preservation standard**: Preserve knowledge, conditions, roles, and associations whose provenance is explicit or whose behavior has already been promised; duplicate wording may be removed, but facts must not be removed. Do not re-run extraction admission audit, and do not add assessment obligations because the source did not separately state details such as `NULL` or granularity. Read `evidence` only when the current judgment lacks a specific prerequisite.

## 1. Common Fields

| Field | Integration requirement |
| --- | --- |
| `name` / `aliases` | Choose a stable business name, put other genuine synonyms into `aliases`, and deduplicate. Different roles or definitions cannot become equivalent through aliases; branch-exclusive names stay in their branch. |
| `description` | Merge compatible descriptions and preserve necessary scope and roles; do not excerpt case by case, and do not replace matching, calculation, or behavior in type fields. |
| `tags` | Deduplicate, normalize, and preserve valid retrieval meaning; compatible tags may be unioned. Tags do not replace business facts, and conditional tags are not broadcast to all branches; use `[]` when there are no valid tags. |
| `properties` | Use only fields and types from this round's `node_schema`. Do not concatenate conflicting strings into arrays or "X / Y", and do not add undeclared fields such as `parameters`; when historical fields cannot express something, report a contract issue. |

---

**Optional fields**: Omit unused `properties` fields. `default:null` in field descriptions means absent by default and does not mean explicit null is allowed; fill it only when the actual type allows null. `NodeContent.description` may be null.

## 2. Integration Decisions

| Input relationship | Handling |
| --- | --- |
| Equivalent or duplicate | One complete version, merging provenance and meaningful names; do not create branches for wording differences. |
| Compatible and complementary | Form a complete definition, preserving different roles, conditions, and associations. |
| Safely generalizable | Clarify common meaning, parameter roles, and bindings in existing fields; substituting the original inputs can restore the original behavior. |
| Well-founded conditional variant | Conditionally integrate when preconditions are identifiable and existing fields can express them accurately; do not fabricate boundaries with case IDs. |
| Actually incompatible within the same scope | Preserve complete `alternatives`; do not vote, splice across branches, or mechanically compute a Cartesian product. |
| Compatibility unknown | Read sources as needed; if still uncertain, report missing prerequisites, and do not default to conflict. |
| Actually different identities | Partition the original members into justified identity subgroups and unchanged singletons. Integrate duplicates within each subgroup; do not force distinct identities into one definition. |

**Generalization boundary**: Sources must support the parameter and its value range; existing commitments such as fixed specifications, business thresholds, counting objects, granularity, deduplication, `NULL`, and tie-breaking strategies must not be abstracted away. Two different values alone are insufficient to infer that any value is valid. Parameter meaning must be written into the published content and cannot be explained only in `explanation`.

## 3. Associations Embedded in Content

Relationships, hierarchies, or pairing facts already present in node content still need to be preserved. Complete source routes do not replace content preservation, and do not prove that relationship semantics are correct; see the role module for specific relationship responsibilities.

**Local correction**: An invalid self-relation produced by a proposed merge is not proof that the members have different identities. Compare their meanings and evidence. Either remove an erroneous or redundant internal relation while preserving supported knowledge and provenance, or refine the partition into justified subgroups and singletons. Neither correction authorizes changes outside this group. The accepted graph changes only after structural validation and independent Assessment both pass.

## 4. Result Recovery Mode

Normal rounds use the result tool specified by the role. Do not emit analysis or repeat the input before tool calls; put concise explanations only in structured result fields. Only when the controller explicitly provides `result_only_recovery.mode="plain_json"` and `tools_available=false`, return directly a complete JSON result conforming to that schema, without calling result or read tools; already-read evidence need not be fetched again.
