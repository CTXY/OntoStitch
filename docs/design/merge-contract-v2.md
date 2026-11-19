# Merge task contract v2

The reviewed design retains Identity, content and relation Writers, and independent Assessment. They share task state, reads, patch compilation and review findings. Identity produces a plan; semantic member collapse and source dispositions occur only while compiling an explicit complete group integration. The accepted graph moves only after the actual candidate passes mechanical validation and independent review.

The graph sent to a model remains compact. Full focus content, the exact structured fields being replaced, resolved alternatives and directly relevant physical identifiers must be present. Neighbours use summaries. Physical identifiers include table and column names; omitted fields are not presented as absent. Writers and Assessments use the same lookup/read semantics. The implementation must not repeat a complete source graph and candidate graph merely to expose one changed relationship.

## Review corrections

- The earlier free-text `shared_assertions` proposal is not used. Conflict nodes can retain `common_sources`: sources explicitly attributed to knowledge preserved in every complete alternative. Shared knowledge remains in complete `NodeContent`; the source field is not a replacement for semantic content or an instruction for SQL matching. A source in this collection need not independently prove every stronger branch-specific assertion. Common and branch support retain distinct roles in model views and resolved consumers.
- Empty common source data must retain the previous serialized graph shape. Adding a default empty field to every node would change old graph fingerprints even if meaning remained unchanged.
- Old relation repair checkpoints contain accumulated unaccepted candidates. They cannot be interpreted as complete new patches relative to the accepted base, nor can old accepted reviews authorize a new contract.
- Legacy engine modules still contain shared helpers and remain referenced by review scripts. Shared helpers must be moved before retiring a path. Deleting an entire file by its legacy name is insufficient evidence that it is unused.

## Controller-owned task state

`integration/task_state.py` defines a versioned `TaskState` with the task ID/phase, fixed accepted base and hash, draft and draft ID, last valid candidate and hash, issues, review, recovery marker, and contract versions. The model does not calculate hashes or optimistic storage revisions. `WorkspaceStore` validates that a noncommitted task base equals accepted `current`; a committed task must instead name its accepted candidate. A review addresses exactly the current candidate and cannot accept an unresolved draft.

`ContractVersions` separates graph/ontology schema, public tools/results, actual role prompt assets, review policy and task-state format. Graph schema includes the `GraphState` model schema in addition to the ontology definition. Public contract fingerprinting uses the registered read tools plus Identity/Patch/Review result schemas. Deliberate semantic policy changes require their own version change; an endpoint or wire retry does not change knowledge acceptance policy.

The input fingerprint remains a fact about existing/incoming graph data, ontology schema and evidence. It is not rewritten to make a new runtime accept old data. Prompt/tool version validation is separate.

## Explicit checkpoint migration

Normal legacy state is never silently read using the new draft protocol. The migration helper creates a new workspace file outside the source checkpoint directory. It preserves accepted `current`, lineage, history, accepted revision, identity/read proof, content plan, relation coverage and the original input fingerprint. It archives the complete original payload, including old unaccepted candidates and reviews, in `migration_source.json` with source hashes and revision.

An old pending candidate is conservatively marked `needs_rebuild`, with no candidate acceptance or old review reused. Only its unfinished local task is reopened from accepted current. This avoids guessing source attribution, retirement semantics, or cumulative patch composition during migration. The archived candidate remains available for inspection, not as an accepted graph. Relation coverage remains recorded; reuse still depends on compatibility of the review policy and region signature.

Training-output migration copies case input/state/status artifacts and audited completed batch artifacts, preserving batch `input_fingerprint.json` bytes. It does not copy credentials, worker configuration, or model wire logs. Existing original logs remain at their original paths. This is a new output, not a change to a frozen runtime or its manifest.

```sh
.venv/bin/python scripts/migrate_merge_checkpoint.py \
  --source-output /absolute/old/database-output \
  --output /absolute/new/database-output

.venv/bin/python scripts/run_resilient_training.py \
  --mode merge --output /absolute/new/database-output \
  --config /absolute/config.yaml --keys /absolute/local.keys \
  --merge-checkpoint-migration /absolute/new/database-output/merge_checkpoint_migration.json
```

Migration performs no model calls. The runner requires the explicit plan for a migrated workspace, verifies its destination and provenance, and still runs normal source/input fingerprint checks. Completed audited batches are reused. A genuinely changed graph or evidence needs a new input run, not a rewritten old digest.

## Validation

The recovery tests cover stale reviews, tampered base/candidate/draft hashes, workspace-to-task ownership, contract mismatch, original checkpoint immutability, old review archival, accepted progress preservation, completed batch/fingerprint reuse and explicit runner plan enforcement. Real failed checkpoints must also be migrated into disposable copies for offline inspection. Model success and semantic correctness require subsequent live local-task validation; a successful migration is not an Assessment accept.

## Compilation and review boundaries

Each complete Patch is compiled from the fixed accepted base with fresh write scope. Reading or receiving an Assessment reference does not grant new root permissions. Explicit coupled edits may extend only that draft's scope. Failed source validation restores the prior candidate, and a failed durable commit restores accepted in-memory state while retaining the reviewed `ready_to_commit` task for recovery.

Ordinary members joining a conflict require explicit branch/common dispositions, just like existing branches. Existing common support is not automatically extended to a newly added alternative. Inherited outgoing edges follow these declarations; branch IDs are qualified by their old owner. Direct alias projection of a conflict now fails before dropping evidence and requires `integrate_groups` instead.

Node and relation reviews include declared consumers affected by changed meanings or operands, even if those consumers' own fields did not change. This review scope is recorded separately from write authorization. An unchanged rejected candidate is not independently assessed repeatedly.

`needs_context` retains the compiled candidate and resolves the exact requested input/before/candidate references. Only Assessment is retried; no Writer is rerun or graph edited to satisfy missing context. An unavailable reference or repeated unsatisfied request becomes an explicit blocked task. Supplemental context and the candidate-bound review survive restart.

The unused relation-specific model loop, JSON Pointer repair protocol, message-position graph references and accumulated pending snapshot reader have been removed. Shared legacy graph/compiler helpers that still serve explicit review/ablation entry points remain; their conflict reintegration path rejects historical common-source data rather than creating a second attribution protocol.
