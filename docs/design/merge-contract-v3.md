# Conflict/alternative task contract v3

This implementation changes the model-facing merge protocol, not graph storage. A conflict remains one owner plus complete resolved alternatives, compacted by code into common fields and whole-field overrides. The model never writes overrides or SourceReference objects.

## Sources and operation boundaries

`integration/source_units.py` enumerates normal content, every old alternative (even without provenance), and an old common scope only when `common_sources` actually exists. Handles bind the accepted baseline hash, operation owner, node, kind and old alternative, and survive unchanged-baseline restart/member reordering. `source_unit_manifest` is included in the Writer task; `read_source_units` supplies coupled owners on demand, including the complete planned group when a member is requested. Physical catalog objects cannot be edited by reading their units.

Every existing input unit has exactly one `source_routes` entry. A destination is `result`, named `alternatives`, `common`, or an explained old-alternative `discard`. Normal/common knowledge cannot be discarded. A route declares preservation; independent semantic review still compares the actual contents. Common support preserves the source unit's knowledge in every complete version and is not blanket support for stronger branch claims.

Planned members can only be completed by their group's `integrate_groups`, whether the result is ordinary or conflicting. Standalone `conflicts` rewrites complete versions; `resolutions` selects one existing version with reasons for every other version. These operations cannot overlap an owner or bypass an identity member. Complete owner operations also prohibit conflicting node CRUD/retirement in the same Patch. All checks occur before scope or accepted graph mutation. Source-route errors identify the owner, path, missing/extra/duplicate IDs and required units.

The public model protocol has no `branch_dispositions` compatibility alias. `legacy_patch_adapter.convert_legacy_patch` is an explicit offline fixture adapter; it preserves illegal historical scopes as invalid handles so regression diagnostics are meaningful. It is never invoked during model submission or checkpoint recovery.

## Loaded policy

`llm/prompts/conflict_contract.md` is loaded by every non-Identity role, in addition to its stage-specific prompt. `read_guidance(conflicts)` returns this same asset. It defines semantics, operations, sources, edges and examples for duplicate, complementary, unresolved and fully reconciled versions. Prompt/tool versions hash the actual asset, read tools, source-unit adapter and public schemas. Task-state version is 3; old checkpoints require explicit migration into a new output.

## Requests and iteration

Planning and actual Writer/Assessment requests use the same idempotent `compact_node_packet`. Equal source/current records are shared by explicit packet-local IDs; same-ID different versions remain separate. Full focus and evidence are not truncated. Every actual request checks messages plus available tool schemas before transport.

`TaskDialogue` rebuilds each active request from stable task input, exact unique read results and the latest complete rejected draft/error. Previous submissions remain in provider/audit traces, rather than accumulating in active messages. Exact draft echoes and repeat reads are suppressed; changed views/results remain readable. Tool pairs remain closed, including parallel reads and representable malformed calls. Missing-name/ID malformed calls receive plain format feedback. Fallback uses the same result schema. Repeating the identical rejected draft/error twice is nonretryable `TaskNoProgress`; different repairs retain the configured call budget.

Mechanical errors (`issues`) are separate from persistent `semantic_issues` and `semantic_issue_basis`. A new draft, a compiler failure or a context request does not erase prior semantic obligations. Historical premises carry their candidate hash and are not labelled current facts. New Assessment accept clears them. Partial review slices with concrete revise findings retain them when another slice needs context. Required context is merged rather than overwritten. A read argument failure does not overwrite the saved draft compilation error.

The task baseline remains fixed. Each complete draft replaces the previous one relative to that baseline. A retained last valid candidate is not the latest invalid draft. Candidate-bound assessment and durable commit gates remain unchanged.

## Recovery and preservation

This change does not rewrite original SQLite files, accepted graphs/history, completed task IDs, input fingerprints, frozen workers or running conversations. Existing explicit checkpoint migration archives the old unaccepted draft/review and rebuilds only its unfinished task from accepted current. Contract versions must not be edited in place to bypass migration.

No real model invocation or worker restart is part of the offline implementation validation. Passing compilation/source audit is not semantic acceptance or final batch publication.

## Portable real regression

`tests/fixtures/thrombosis_contract_failure/` contains gzip copies of the accepted task baseline, open task, five actual failed proposals, original initial packet and provenance hashes. Credentials, HTTP headers, response narration and key files are excluded.

- Original proposals 3/4/10 report zero missing units and the extra non-existent owner common scope.
- Original proposals 6/12 fail `identity_operation_required` instead of replacing the original member obligation.
- Explicit conversion plus removing the extra common scope and redundant owner rename allows all 14 groups to compile and pass source preservation audit. It does not assert final graph validity: relation reconciliation still has grounding contests to process.
- Actual first packet sharing saves 56,347 serialized characters.
- Repeated real drafts remain latest-only in the active context; original evidence and necessary exact reads remain intact.

Tests additionally cover source-less history, owner-qualified/base-bound handles, illegal discard, overlapping operations, semantic findings across SQLite restart, full candidate comparison, malformed recovery and actual model-facing packet projection.
