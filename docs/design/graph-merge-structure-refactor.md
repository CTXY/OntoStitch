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
