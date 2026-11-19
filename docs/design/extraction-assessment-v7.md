# Extraction assessment v7

The former contract required most changed nodes and dependency edges to demonstrate
SQL application independently. A supported but unused unit could reject a whole
case even after three correct SQL replays. The new contract separates:

1. **Ontology quality.** The assessor still judges the exact type-specific quality
   checks. The overall label is derived from these checks. SQL success cannot
   conceal a bad definition, binding, operand, scope or relation.
2. **Injection.** Code records actual generation-context presence for each attempt.
   Direct selection is unnecessary when context presence/dependency inclusion is
   recorded. Raw recall alone is not injection. A recalled/selected unit or a unit
   assigned to a fixed repair target requires observed injection; otherwise a
   supporting unit remains explicitly uncovered. Removed units need no injection.
3. **Related SQL errors.** The assessor reports unresolved fixed targets, new
   related errors or supported regressions. Unit-level SQL errors cite their
   affected unit, current attempt IDs and a verbatim SQL excerpt. Non-use, lack of
   an ontology ID in SQL, or an equivalent spelling is not an error. Unrelated
   SQL errors do not independently invalidate a unit. Attribution uncertainty is
   retained as uncertainty, rather than a confirmed graph regression.

`UnitResult.application`, application options and type-based SQL-application
requirements are removed. Responses containing the old protocol are rejected.
Target and regression feedback use separate types, so their JSON schemas express
exactly the allowed `kind`. Locations enumerate supplied graph IDs, separately
from target IDs. The model cannot author `retrieval_results`; these are derived
and checked against saved facts and included in acceptance evidence.

The independent SQL gate still requires three consecutive correct replays.
Accepted revisions, reference checks, coupled groups and source preservation are
unchanged. A changed subgraph still needs new matching SQL and assessment proof.
This revision does not automatically accept any archived failed graph.

## Relevance filtering during extraction

`services.sql_training.retrieval_relevance_filter` defaults to `false`. Extraction
passes it to its own retrieval service. In this mode every recalled representation
is retained without calling the relevance model, followed by ordinary dependency,
conflict and JOIN-context completion. Recall limits still apply; the entire graph
is not injected. Invalid/untrusted physical facts are not made valid by this mode.

Online and CLI retrieval retain relevance filtering by default. The mode is part
of the retrieval checkpoint identity. Switching it cannot reuse cached relevance
judgments from the other mode.

The original three-database audit counted 1,149 retrieval runs (including repeated
validation/final-baseline runs): 33,453 candidate-node appearances, 14,487 rejected
appearances, and 10,310 candidate appearances absent from final context. Mandatory
closure restored 4,177 rejected appearances. Semantic nodes appeared 5,682 times;
79 were excluded, comprising 17 distinct case/node pairs across 9 cases. These are
historical appearances, not 79 unique concepts and not a claim that every rejection
was wrong. The detailed audit is in the run's `analysis_review/relevance_filter_audit.json`.

## Model invocation failures

The model call and SQL interpretation have separate exception boundaries. An
exception before obtaining a model response is unavailable, never evidence that
SQL or ontology content is wrong. Known connection/timeout/protocol/stream errors
and retryable HTTP statuses can be retried within the existing bound. Authentication
and unknown invocation failures stop as unavailable without blind retries.
A known unfinished response is also unavailable; a completed empty/invalid SQL
response remains a semantic generation failure.

`OntologyGenerationUnavailable` carries safe error type, HTTP status and retryability.
`GenerationUnavailableReason` preserves these in-process; the evaluation writes
ordinary JSON `infrastructure_errors` before string serialization loses the type.
The case's unavailable status remains distinct from incorrect SQL and is not
promoted to passed. No upstream secret-bearing message is copied into these fields.

## Checkpoints and running batches

The assessment policy ID is `quality-injection-related-sql-v7:<fingerprints>`.
Existing frozen background runtimes are not rewritten. Old v6 results and accepted
revisions retain their original meaning. Fresh v7 runs use isolated output paths;
there is no policy alias that relabels old failed results as passing.
