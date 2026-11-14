# Merge runtime: evidence-preserving context reduction

## Observed failure modes

A five-case batch took51 HTTP/model calls:1 identity,12 consolidation,10 node assessment,22 reconciliation and6 relation assessment. The model responses took906 seconds in total; a gateway protocol exception and subsequent recovery added1430 seconds of downtime. Recovery repeated an unaccepted node task, not the published graph. Its canonical input contained identical archived/current records and tools returned already supplied objects and evidence again. A later relation task twice tried to retarget a physical grounding edge to a Concept while mistakenly describing it as a dependency edge; terse validation feedback did not identify the actual predicate/types.

## Changes

- Preserve the complete internal task/graph payload. In the model packet only, represent exactly equal archived/current records once, with explicit `source_equals_candidate` IDs. Different versions, descriptions, branch scope and provenance are not collapsed.
- When a read returns an identical complete supplied object, return its ID/location instead of repeating its body. Candidate scores/signals remain. Do not suppress newly changed objects or absent evidence.
- Node evidence reads identify evidence already in the initial packet. Relation field/guidance reads reuse earlier results within the same unmodified local transaction. No semantic read is replaced by an inferred fact.
- Every inspected edge includes its actual predicate and endpoint types. Invalid endpoint errors identify the actual ID/type and the permitted types from the schema. Metric refs to physical inputs use grounded_on; refs to Concept/Metric use depends_on. No constraint is relaxed and no edge is automatically semantically rewritten.
- Node Assessment reviews node-owned meaning/binding/formula/behavior, not a preferred intermediate edge layout. Shared-endpoint ownership and redundant assertions are structural signals handled by the following Reconciliation phase; signals include all claimant edge IDs so a repair is not limited to one example edge. Intrinsic formula defects still block node acceptance, and final graph constraints remain mandatory.
- Use ten case graphs per operator invocation, with all source mappings and per-batch accepted checkpoints. Keep previously accepted batches. Agent-internal groups/regions remain independent of input-case partitioning.

## Validation limits

Exact-record sharing can be expanded losslessly. In the captured input, the consolidation user packet fell from168533 to141312 characters; one candidate-read result fell from70340 to41412 characters. These are measured serialization reductions, not a claim of identical model decisions or guaranteed token/call savings. Actual graph quality, conflicts and provenance still pass the existing assessment/audit gates. Semantic rounds stay bounded as configured; no extra model-call cap is introduced.
