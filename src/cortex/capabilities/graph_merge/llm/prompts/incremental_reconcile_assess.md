## Batched Reconciliation review

Return `BatchNodeReview`: one `{unit_id, review: NodeReview}` per `review_units` entry. Use each entry's exact node/deletion/decision/reason target lists. Shared graph and evidence appear once. `unit_patches` identify unit changes; current definitions and complete alternatives are in the shared packet.

`reviewed_edge_ids` are relation targets, including deletions available in `accepted_before`. Check their business role, actual bearer, direction, scope, evidence and preservation. Their unchanged endpoints are CONTEXT: do not re-audit unrelated old node claims or Rule reasons. A concrete endpoint defect matters only when it invalidates the targeted assertion. Actual node/dependency changes still receive full node review.

Judge each unit against the ACCEPTED baseline, independently of other proposed units. If a unit relies on another candidate repair, report `cross_unit_issues` with involved unit IDs and concrete missing premise. Do not accept one merely because all candidates together look correct. Code will keep coupled repairs atomic.

Report all discovered material defects in the supplied unit in one review: action atomicity, affected direct consumers, supported explanations, and before/after knowledge preservation. Read the whole action: choosing fields and arranging them are separate decisions even in one sentence. A generalized ranking/layout Rule must not silently erase its former independent selection behavior. This is preservation of supplied knowledge, not a demand to invent rules for every SQL clause.

Do not re-audit unrelated retained Rules. A no-edit unit may still require review for a named unresolved joint decision. A layout must work with every applicable selection branch unless explicitly scoped. Prior corrections are not evidence, but do not reverse them without explaining the evidence error.

Restoration requests need support from the supplied QA/SQL or physical metadata, not merely an archived node's prose or an earlier review. Preserve supported business meaning without restoring unsupported profiling, priority or execution claims.
