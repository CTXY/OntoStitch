# Problem-scoped Graph Reconciliation

## Scope

This revision changes only the structural reconciliation stage. Identity retrieval, matched-group batching, consolidation and its independent Node Assessment remain unchanged. No school-specific predicates, case numbers, column names or SQL outcomes determine production repair actions.

The previous four-case run used 27 Reconciliation calls plus 21 Assessment calls in this stage (48 total; 65 over the complete merge). Three final no-change tasks used 14 calls, but these are not all assumed removable. A first semantic check may still be necessary. End-to-end savings require a real fixed-version comparison.

## Algorithm

1. Cover new/integrated/directly affected business content once, together with applicable structural signals. Signals recall potential problems, not semantic conclusions; they cannot detect every malformed Rule or lost decision.
2. Build a shared local packet and budget its actual serialized size. Keep complete branches, binding competitors, declared relationship groups and necessary direct consumers. Shared background is read-only and does not recursively expand its entire neighbourhood. An oversized indivisible check runs alone.
3. Reconciliation judges every named inspection and routine target, then returns complete current-relative repair units. It can conclude no change, propose a repair, or explicitly fail when required evidence cannot be retrieved. A missing patch is not a semantic proof.
4. Code groups units with actual write/read or disposition dependencies, plus Agent-declared semantic coupling. Shared read-only parents alone do not couple units. Each component must independently apply to the accepted baseline; the combined candidate is checked too.
5. Batch Assessment reviews actual semantic changes, deletions, concrete business-role changes, fresh evidence and explicit related-decision/source-selection inspections. Unchanged unrelated Rules are not mandatory review targets. Complete ordinary no-change inspections skip a second review. Rejected semantic feedback cannot be closed by returning an empty patch.
6. Independently accepted units update the workspace graph and lineage together. Failed units retain their proposed patch and current feedback; the next invocation contains only their required scope. Mechanically or semantically coupled units pass/fail together. No dependency DAG or speculative execution is used.
7. Recompute remaining signals against the accepted graph after each batch. Resolve organization before standalone abstract-predicate wording: an organization repair may retire the old edge entirely. Run-local check coverage is invalidated by changes to the checked objects, incident facts or relevant contracts. New patterns receive explicit conclusions. This is not a global semantic cache or a claim of inferred correctness.
8. Final publication and complete public provenance audit run once. An incomplete/rejected run does not publish a final graph.

## Input and output

The packet uses existing current/source views: `candidate_nodes` and `candidate_relations` are current, `nodes` and `relations` are immutable origins. Exact identical source definitions may be referenced through `unchanged_source_objects`; this is explicit deduplication, not evidence omission. Relevant evidence and guidance appear once. Display-only directories are removed; tools still retrieve complete objects in an explicit view.

Writer input contains `routine_review_ids`, `inspection_signals`, current local objects, relevant originals/QA, contracts, writable targets and `new_id_prefix`. Retry adds only failed `retry_units` and their feedback. Writer output is:

```text
ReconciliationBatch
  units[]
    unit_id
    checked_node_ids[]
    patch: BatchResult
  coupled_units[][]
  failure?
```

Each routine target and named inspection must be answered exactly once. Patches are complete replacements for failed units; there is no invalid-pending accumulation or `remove_*` protocol in this path. Code preserves the existing current-relative patch validator, source checks and conflict compaction.

On retry, feedback-referenced current objects are promoted into the refreshed routine target list; validation uses that refreshed list, not the original batch's list. Rejected newly created objects remain in complete `retry_units` rather than becoming nonexistent accepted-graph targets. A changed inspection must affect its actual problem, not merely an unrelated object. A no-change claim cannot override an explicit hard relation constraint.

Assessment receives a shared candidate, exact per-unit target lists, actual-change directories, accepted-before contents for changed objects, source evidence and compact unit patch directories. Complete candidate definitions are not duplicated in the patch directory. It returns:

```text
BatchNodeReview
  results[]
    unit_id
    review: NodeReview
  cross_unit_issues[]
    unit_ids[]
    issue
```

The existing per-Rule decision/reason coverage and exact-current reason quote checks still apply separately to each unit. Assessment must judge each unit against the accepted baseline, not rely on another unaccepted candidate. Cross-unit findings block the named units and join their retry. Findings should cover all discovered material problems in one response; this does not guarantee model exhaustiveness.

## No-change boundary

- Complete ordinary check, no unresolved feedback: record a Reconciliation conclusion, skip independent Assessment.
- Semantic repair or explicit open decision comparison: targeted Assessment, even if the latter produced no edit.
- Missing necessary facts or unresolved rejection: retrieve/extend or report incomplete; never mark complete because the patch is empty.

Code checks coverage and references, not whether a no-change business judgment is true. Skipping ordinary double review is a cost/quality trade-off evaluated offline; it is not labelled independently validated.

## Administrator illustration (not production rules)

Cases 36/63 exercise complete all-slot/first-slot choices and their name-layout consumer. Changing a slot policy and repairing an incompatible consumer is one unit; a first position does not establish a primary role.

Cases 35/87 exercise email ownership and field selection versus output shape. A Concept admitting Email1/Email2 does not prescribe the chosen subset. Splitting a compound Rule must preserve each supported independent choice; retaining the QA archive alone is not an explicit ontology representation. Which differences are conflicts, conditional or distinct decisions remains an Agent judgment from evidence.

Charter-number ownership can be independent when it uses accepted background. No declared UNIQUE constraint does not establish observed duplicate values. A School/Administrator/Email triangle is only a signal; contracts and actual attribute bearers decide redundancy.

## Implementation and evaluation

- `integration/reconciliation.py`: serial task selection, staging, conditional batch review, partial acceptance and retry.
- `integration/reconciliation_scope.py`: run-local coverage and explicit source/current deduplication.
- `integration/reconciliation_units.py`: minimal output models and undirected dependency grouping.
- `llm/incremental.py`: batch result tools and exact per-unit review validation; the matched-node path retains its existing output/prompt.
- `llm/prompts/incremental_reconcile_batch.md` and `incremental_reconcile_assess.md`: centralized Markdown instructions.

Controlled tests cover no-change, mandatory decision review, rejected no-op bypass, partial acceptance, shared background, new-object dependency, business-role edits, source-safe deletion and refresh of obsolete checks. They test orchestration and invariants, not model quality. A subsequent official-endpoint run must start empty with the same four original graphs, no imported candidate or targeted feedback. Report full phase calls and input/output/cache tokens, including any failed runs, alongside semantic and public provenance review.
