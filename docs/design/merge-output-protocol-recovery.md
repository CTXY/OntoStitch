# Merge output recovery and canonical task context

## Status

Implemented in source and verified offline. The existing production runtime,
original checkpoints, published batches and accepted transactions were not
replaced. No new real model validation is claimed by these regressions.

The implementation was reviewed by three subagents, covering draft/diagnostic
presentation, context-dependent progress, and checkpoint/Assessment recovery.

## State and operation boundaries

| State | Meaning | Rules |
| --- | --- | --- |
| `TaskState.base` | Accepted task baseline B | Fixed throughout the task. |
| `draft` / `draft_id` | One complete current submission D | A unique JSON-object result replaces it, even when schema repair remains necessary. |
| `last_valid_candidate` / `candidate_draft_id` | Successfully compiled C and its originating D | A retained older C does not prove the new D compiled. |
| `issues` / `issues_draft_id` | Mechanical feedback for an explicit D | Same-D recording preserves it; a new D clears it without clearing semantic findings. |
| `semantic_issues` / `semantic_issue_basis` | Outstanding semantic findings and exact historical premises | Preserved through schema/compiler failures and process restarts. |
| `agent_recovery` | JSON recovery counters, visible reads, mode and bindings | Persisted atomically with the task, separately from graph knowledge. |

Legacy v3 checkpoints may load missing optional bindings without inventing them.
An unbound candidate cannot receive a new review directly: saved D must first
recompile to exactly the saved C. A legacy accepted-but-uncommitted review also
requires this recompilation. A changed prompt/tool contract still requires the
existing explicit migration process; version hashes must not be relabeled.

## Canonical request construction

`TaskDialogue` reconstructs each request from a stable task packet, one current
D, version-bound errors, outstanding semantic findings/basis and exact read
exchanges. Old full result actions are archived rather than replayed. The
`repair_has_draft` location flag and old repair-exchange accumulation are removed.

A complete `inspect_draft` echo does not append D again. Its fresh diagnostics
are transferred independently, including an explicit empty list for a matching
D. Projections inherit the full D identity. Old or explicitly unbound diagnostics
remain visible as historical observations and cannot overwrite current bound
errors. Equivalent semantic findings are not repeated as mechanical errors.

Compiler-supplied exact records are pinned once in `repair_facts`; diagnostics
use `repair_fact_ref`. Changing D or replacing error text does not discard these
records. Candidate or unqualified historical records retain their source binding
and cannot establish the content of the current C. Bare object correction IDs
and missing-source diagnostics do not count as newly learned business facts.

## Response preflight and repair

Read-only calls may run in parallel. A final result (`submit_patch`, `Review`, or
`IdentityDecision`) must be the only call in the response. Missing/empty IDs,
duplicate IDs, unavailable tools and malformed/mixed result calls are rejected
before any tool, draft persistence or compile callback executes. JSON arrays,
non-object JSON and non-finite values cannot replace D.

Protocol recovery is separate from content validation:

1. First invalid response: retain D, archive the raw response, send concise
   protocol feedback.
2. Second invalid response: retain D and enter submit-only recovery (or the same
   output schema in plain-JSON fallback).
3. Third invalid response: `OutputProtocolRecoveryExhausted`; checkpoint retained.

A valid standalone known result remains schema-validated even if a gateway
returns it during plain-JSON fallback. It is not accepted without validation.
Protocol exhaustion remains blocked across restart, even with new facts.

Schema, Patch compilation, identity validation and Assessment-output validation
use separate stage names. Their no-progress key includes the entire rejected
output set, exact feedback and actually presented fact/policy fingerprint. One
response is one observation; transport retries are not decisions. Repeated empty
placeholders cannot count as two decisions or conceal a different proposal.

The fact fingerprint is captured before the model call. A new error's supplied
facts belong to the next request. Real new visible facts permit a new attempt;
call IDs, timestamps, repeated reads, erroneous field selections, ordering and
changed error prose do not. Explicit new facts may unblock content no-progress
across restart while preserving earlier failure buckets. Protocol exhaustion is
not unblocked this way.

## Fact identity and context budgets

Visible records are normalized into atoms by view, object ID and selected field
content. Packet fields and later equivalent projected reads share fact identity.
Evidence, source units, contracts, historical basis and full empty-relation query
qualifiers remain represented. Different views and changed field values are
never collapsed. No graph/evidence content is truncated.

Request limits still apply to the actual serialized messages and tool schemas.
Each request now logs estimated system/task+graph/draft/diagnostic+fact/read/tool
components, alongside its exact total character count. Oversized requests fail
before transport with non-retryable `RequestInputTooLarge` and component details;
existing dependency-complete review splitting remains responsible for scope.
Characters are not reported as model token counts. Existing configured call
budgets continue to govern read-only exploration; this change does not invent a
new semantic stop rule for every repeated read.

## Durable recovery and Assessment

Agent recovery envelopes bind task, B, C, role and policy, and validate tracker
and dialogue hashes. Missing/null/empty tracker records are corruption, not a
request to reset counters. Restoring a malformed read/action or diagnostic fact
reference fails before applying changes. Restored model-visible candidate reads
also restore compiler visibility; reads still do not authorize unrelated writes.

Candidate visibility supplied by a compiler context error is saved with its exact
candidate hash, allowing recovery without replaying an already supplied scope
failure. Invalid visibility IDs/snapshots are rejected.

Assessment caches are checked before model invocation. They bind baseline,
candidate, actual visible packet/read facts, source input and policy. A new fact
already injected in the request invalidates reuse. Node Assessment prepares and
checks every slice; it never treats the final slice as the whole review.
Outstanding findings and historical basis discovered during a `needs_context`
iteration are supplied to subsequent slices and retained across restarts.

## Real regressions and preservation

Portable fixtures in `tests/fixtures/merge_output_protocol_failure` contain:

- thrombosis requests 33/34: duplicate checkpoint-draft echo;
- thrombosis request 64: multiple result bodies in one repair response;
- toxicology request 47: raw SSE calls `{}`, P, `{}`;
- exact checkpoint payloads: thrombosis rev75/history2/assessing and toxicology
  rev61/history47/validating, including original semantic findings and lineage.

The real toxicology P also contains unsupported `explanation_alt_note`. Replay
first tests multi-result feedback, then schema repair. Scripted responses and a
stub compiler isolate this protocol path; they do not claim model-generated
semantic repair. Checkpoint regressions separately prove accepted current,
history and lineage are preserved and no legacy candidate binding is invented.
Real earlier thrombosis source-route fixtures continue to run through the real
compiler in the related suite.

Final verification: 553 related merge/integration tests pass, plus static lint
and diff checks. All 448 frozen runtime file hashes are unchanged. Original
thrombosis/toxicology checkpoint payload hashes match the captured fixtures.
The implementation report is under `output/merge_output_protocol_complete_20261119`.
