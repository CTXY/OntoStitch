# Conflict and alternative contract

## Meaning and reads

A conflict owner has one business identity and several complete, coherent content versions. Alternatives are not independent GraphNodes. Compare full resolved contents, including scope, matching methods, calculations/actions, conditions and consumers. Code compacts storage: it stores equal fields once and variable whole-field overrides; the owner may contain a placeholder name/null fields. A storage placeholder is not missing knowledge or another ordinary input version. Never write overrides or internal source dictionaries.

Distinguish equivalent wording, compatible complements, evidenced conditional differences, incompatible content under overlapping conditions, and a genuinely different identity. Merge equivalents and compatible complements. Express a conditional reconciliation only when the conditions have evidence; do not invent boundaries. Keep unresolved incompatible complete versions. Different identity requires revisiting Identity, not forcing a conflict. A source vote, the current question's preferred answer, or equal physical targets does not resolve competing accepted knowledge.

## One operation for each owner

The task supplies source_unit_manifest with required_operation and exact input source_units.

- An owner in a planned identity group must use integrate_groups with the full planned member_ids, whether its result is normal or conflicted. conflicts/resolutions cannot replace that integration.
- A standalone existing owner uses MergePatch.conflicts to construct/rewrite complete alternatives, or resolutions to keep one existing alternative with explicit reasons for every discarded alternative.
- One owner may appear in only one of integrate_groups, conflicts or resolutions. Do not chain these operations for one owner in a single Patch.
- A new owner has no historical source routes. Creation still requires its evidence and complete content.
- If a coupled existing owner is not in the initial manifest, call read_source_units(owner_ids=[...]). Reading scopes never authorizes unrelated writes.

## Exact source units and routing

The controller enumerates real units from the fixed before graph: normal_content, each old alternative, and common only when original common_sources exists. Identical fields across branches do not create another common unit. Copy unit_id exactly; do not reconstruct node/null tuples or invent source units. IDs belong to this fixed task base and owner; never reuse another task's handles.

For integrate_groups and existing-owner conflicts, source_routes must name every listed source_unit_id exactly once, with one destination and a concise explanation:

- {"kind":"result"}: original knowledge survives in the complete normal result.
- {"kind":"alternatives","alternative_ids":["A"]}: survives in the named complete output alternative(s).
- {"kind":"common"}: original knowledge survives in every complete output alternative.
- {"kind":"discard"}: only an old alternative, with a concrete evidenced correction/redundancy reason. Supported competing knowledge must remain. Normal/common units cannot be discarded.

Only alternatives destinations include alternative_ids. A normal result uses result/discard; a conflict result uses alternatives/common/discard. The route is a knowledge-preservation claim, not proof. Assessment compares complete source and result knowledge; source coverage alone cannot establish semantic preservation.

Complete output alternatives use alternative_id, content and evidence_ids. common_evidence_ids preserves original input knowledge in every complete branch; it does not mean those sources independently prove every stronger branch claim. Do not mark an input common merely because one sentence is shared while another assertion conflicts. Code resolves SourceReference objects and retains common/selected attribution.

## Edges and consumers

Routes also guide inherited outgoing edge scopes. Code redirects member IDs and maps old branch IDs to destinations. An old unscoped edge is scoped to the old owner's versions; a newly introduced alternative does not automatically inherit it. Preserve narrower authored scopes and explain necessary coupled changes. Use properties.alternative_ids for a relation valid only in some output versions. Never infer business edges solely from matching targets. Assess affected consumers even when their own fields are unchanged.

## Examples (illustrative handles; copy actual manifest IDs)

1. Duplicate alternatives: S1 says CRE >= 1.5, S2 says creatinine is at least 1.5, S3 says CRE > 1.5. After confirming identical units/population/NULL scope, produce complete A: CRE >= 1.5 and B: CRE > 1.5. Route S1/S2 to A and S3 to B. Record the equality-boundary conflict. Do not create three alternatives or silently pick A.
2. Compatible addition: old A/B disagree on thrombosis code ordering; normal S3 only adds the examination-record grain, verified applicable to both. Keep complete A'/B' with that grain, route old A to A', old B to B', S3 to common. If S3 also asserts one disputed ordering it is not a common unit.
3. No original common source: old owner has A and B, no common_sources, plus three normal members. The manifest contains exactly five units, not an extra owner/null unit. Submit five routes. The common fields stored on the owner do not add a sixth input.
4. Complete resolution: all source knowledge becomes one compatible full meaning. integrate_groups.content supplies that normal result, with result routes for all preserved units. Code removes conflict metadata. resolutions is only for selecting an existing alternative, not synthesizing a new combined content.
5. Wrong operation: planned members X,Y,Z with owner X cannot be completed by conflicts(X). Keep integrate_groups(X,[X,Y,Z]) and return complete alternatives there.

Do not mechanically form a Cartesian product of input alternatives or mix fragments of incompatible versions. Construct only coherent, supported results. Exact duplicate full contents should become one version with combined attribution; semantic equivalence still needs judgment.

## Revision

Every submit_patch is the complete replacement draft relative to fixed before. A rejected draft changes no accepted graph. Use structured code/path/owner_id/missing/extra/duplicates feedback; preserve outstanding semantic findings while correcting mechanical errors. Read only the missing premise or draft section. Do not repeat analysis, the full graph, or previous drafts. Model callbacks/audit logs retain history independently of the active request context.

semantic_issue_basis contains pinned historical records qualified by their outer candidate_hash. A record labelled candidate inside that historical packet belongs to that old candidate, not the current one. Use it to understand prior findings; read the current candidate to judge a repair and never relabel an old record as a current assertion.
