# Graph Merge structure refactor

The supported workflow is `IncrementalGraphIntegrationEngine` with the current
node-local and reconciliation protocols. Old engines, import aliases and old
checkpoint formats are not compatibility targets. Current transaction semantics,
model-visible contracts, provenance and publication remain behavior targets.

## Delivery checkpoints

1. **A — executable baseline:** reproduce failures, distinguish stale test inputs
   from runtime defects, and restore meaningful current-contract regressions.
2. **B — module migration:** move the current modules into their owning packages,
   update production callers, scripts and resource paths; preserve implementation.
3. **C — legacy removal:** remove retired engines and protocols after extracting
   every helper still used by current compilers and SQL training.
4. **D — state ownership:** separate immutable inputs, accepted session state and
   local task execution, including isolated nested relation work.
5. **E — consolidation:** combine signal scope/premises and receipt/ledger logic,
   simplify presentation and eliminate redundant context transformations.

Every checkpoint requires relevant tests, independent subagent review, resolution
of actionable findings, a separate commit and a push to the refactor branch.

## Ownership

- Root `engine.py` and `planning.py`: orchestration and actual production plans.
- Root inputs/evidence/models: immutable inputs and genuinely shared contracts.
- `identity`: retrieval and identity decisions.
- `consolidation`: one complete identity group's content and assessment.
- `reconciliation`: local graph edits, scopes, independent assessment and receipts.
- `runtime`: task state, accepted session, persistence and recovery.
- `graph`: graph operations, shared edit compiler infrastructure, provenance,
  local validation and final publication.
- `llm`: shared transport/recovery/budget/tracing; retain `llm/prompts` resources.

## Invariants

- Node consolidation is one complete group; no legacy multi-group slicing.
- An initial empty preview is not a Writer decision. Complete edit intent,
  compiled preview, assessment handoff and accepted candidate stay distinct.
- Nested relation work never independently publishes the parent node candidate.
- Normal Relation Merger has no independent Assessment; nested suggestions do.
- Accepted graph, lineage, history and receipts are saved atomically and roll
  back together on persistence failure.
- Failed reads roll back their grants, facts and dependency journal.
- Receipt dependencies include complete relation queries, missing objects,
  predicates, evidence and supplementary reads; accepted membership is AFTER.
- Followups do not replay completed model calls after commit/queue-removal crash.
- Local validation may retain but never enlarge existing binding contests;
  final publication remains strict.
- Publication preserves assessed semantics, source coverage, public ID collision
  handling, catalog correspondence and structural rewrite views.
- All model roles share the existing call budget. Prompt bytes, schemas, tool
  descriptions and protocol recovery are not opportunistically rewritten.
- Keep conservative implementation/policy binding; resource relocation must not
  weaken assessment or receipt invalidation.

## External migration checklist

Update package initializers and bootstrap before deleting eager-imported legacy
code. SQL training consumes `PHYSICAL`, remapping, exact deduplication, contract
isolation and `EvidenceItem`. Scan Python imports AND string resource paths,
`files()`, source hashing, AST inspection, report scripts and wheel artifacts.
Retire old experiments explicitly; preserve training integration test intent with
deterministic agents implementing the current contracts.

## Review amendments

- `matched_batches` is not needed by the default node-local path.
- `ActiveBatch`, `BatchResult`, `RelationPatch`, `ProposalScope` and current node
  Review are current internal compiler dependencies, not disposable old APIs.
- Keep business context and model presentation separate; do not concatenate the
  three old node-context layers into a large file.
- Keep Relation Merger service/model protocol/outer acceptance boundaries.
- Signal and obligation kinds belong to the domain contract, not prompt loaders.
- Keep lineage separate from source-unit/coverage code.
- Current single-graph repairs and optional local relation-ID presentation are
  functionality, even though legacy checkpoint migration can be removed.

## Stage A — baseline repair

Initial isolated-worktree run: 29 failed, 614 passed, 4 skipped. The selected
suite after repair: 642 passed, 3 skipped (optional external output fixtures).

The failures were stale authoring/history contracts, incomplete test obligations,
model-binding stubs that mutated the unbound model, and local-only fixtures not
tracked by Git. Production Python was not relaxed to accept those inputs.

- Current scope tests explicitly nominate nodes and edges, including nested
  repair obligations. Assertions cover no unrelated adjacency expansion.
- Recovery tests use independent model bindings and assert Writer/Assessment
  call counts, parent context restoration and no calls after commit.
- Historical source-routing samples are explicitly converted at the fixture
  boundary, preserving original files, exact diagnostics and all 14 groups.
- Removed unsupported metadata authoring tests; retained their current namespace,
  source preservation and indivisible explicit-obligation test intentions.
- Two portable real fixtures are tracked with narrow ignore exceptions.
- Corrected one model-visible Writer prompt sentence: unused compatible
  predicates are available on demand, not all supplied in the first bundle.
  This is an explicit policy-description correction, separate from relocation;
  existing policy hashing correctly invalidates prior prompt bindings.

Independent reviews covered dependency migration, scope/source assertions,
model stubs and nested recovery. No unresolved weakening of current assertions
was identified.

The full repository baseline was also measured: 2,206 passed, 122 failed,
3 skipped. Exact failing node IDs are recorded in `graph-merge-baseline.json`.
This is not a claim of a green repository: most additional merge failures exercise
retired direct-Patch/read-source/history protocols and are replacement targets in
stage C. Online discovery/extraction failures are outside this refactor and overlap
work already in progress in the original workspace. Two budget failures captured
during collection have subsequently been repaired (5/5 budget tests pass).
Subsequent stages must not add failures beyond this recorded baseline; current
transaction/recovery test intentions need explicit coverage before old tests retire.

## Stage B — module relocation

Moved 56 modules into root engine/inputs, identity, consolidation, reconciliation,
runtime and graph ownership. Updated repository imports, module-alias imports,
current SQL training callers, report source paths and conservative source-policy
resource resolution. Legacy engines remain only until their shared functions are
extracted in stage C; this checkpoint does not claim they have been removed.

A 402 KB snapshot of model-visible schemas, role prompts, node packet and relation
messages is byte-for-byte identical before and after relocation. All 2,331 tests
collect. The core run passed 636 cases with 3 optional-data skips; its 11 failures
were a retained old Operator fingerprint reading the moved identity file. After
fixing that resource path, the affected workflow/transaction and SQL integration
group passed all 51 cases. Package/policy smoke and independent AST/import review
found no remaining relocation regression.

The temporary implementation-source map preserves existing invalidation coverage;
it is an internal migration aid, not an old public import or checkpoint adapter.
It will be replaced with current resource paths during the subsequent cleanup.

## Stage C — remove retired execution paths

Removed the Operator, old node-group reconciliation, two old integration engines,
old LangGraph workflows, old role agents and buffered model tools. Current batch,
scope, source-route, validation and publication primitives were extracted first;
their retained function bodies were AST-compared with stage B. SQL training uses
the graph leaf operations directly. Historical regression-data converters live
in test/report utilities, not the production import graph. Old migration scripts
and unsupported CLI options were retired, and current runners use WorkspaceStore.

Only current prompt assets remain. The same 402 KB model-visible snapshot remains
byte-identical to stage A. Conservative policy binding now includes all current
implementation source files rather than a map of obsolete source locators.

Test retirement and replacement are documented in
`graph-merge-test-retirement-map.md`. Canonical recovery tests now cover corrupt
envelopes, failure budgets across restart, new/repeated/failed read facts,
result-only recovery, commit-write rollback and candidate reconstruction failure.
New direct guards cover changed/deleted payload round trips and rejection of
abstract predicates at final publication.

Full run: 2,096 passed, 25 failed, 3 skipped. Three merge test-fixture/old-seam
failures from that run were then corrected and their group plus new guards passed
15/15. The remaining 22 full-baseline failures are the optional external-data
probe, pre-existing shared self-JOIN assertion, and 20 online/rule-guidance cases
outside this refactor. No corresponding production constraint was loosened.
Additional focused results: 375 stage-migration tests, 149 recovery/protocol tests,
77 core transaction tests and 98 affected offline/downstream tests passed (sets
overlap; these counts are not a combined total).

Three independent reviewers approved the stage after the coverage gaps were
filled. The deterministic no-op merger fixture now preserves identical original
descriptions and refuses nontrivial semantic merging instead of inventing prose.

## Stage D1 — explicit accepted session and dependency boundaries

`MergeSession` now owns accepted graph/lineage/history/revision, task progress,
receipts, followups and merger progress. Snapshot/restore/save and accepted-state
rollback have one owner. Engine configuration and Agent/budget remain outside the
Session. Callers use `engine.session` explicitly; no attribute proxy preserves old
engine fields. Input fingerprinting lives with the inputs instead of the engine.

State/hash models no longer import role contracts. Protocol/source binding lives
in `runtime/contracts.py`; common relation obligations, graph views, conflict
operations and error feedback have low-dependency owners. Signal/obligation kinds
are domain constants, not prompt-loader definitions. Extracted functions and
schemas retain their previous bodies and model-visible snapshot is byte-identical.

The broad repository check excluding the 22 documented out-of-scope baseline
failures reported 2,074 passed, 5 failed, 3 skipped and one deselected self-JOIN
case. All five failures were receipt-only test stubs still constructing old engine
fields; their whole file was then corrected and passed 7/7. Focused session/core
and affected recovery sets also passed. Independent review confirmed original
rollback fields, AFTER receipt/history binding, followup ordering, new-run reset,
shared budget and single-graph recovery. Production undefined-symbol lint and
diff checks pass.

Task execution still uses the existing shared context at this checkpoint. Task
forks and removal of nested dictionary save/restore are the next D2 checkpoint,
not claimed as completed here.
