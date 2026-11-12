# Incremental merge: real-model validation

## Experimental scope

- Checkpoint: `e4ffd25`; implementation branch: `codex/incremental-merge-sessions`.
- Provider: official `api.deepseek.com`; model: `deepseek-v4-flash`, thinking/low. LangSmith disabled.
- Inputs: previously extracted BIRD california_schools cases 35, 36, 63, 87. Reverse order changes only source-block order, retaining IDs, contents and relation contracts.
- Run artifacts remain in workspace `output/ontology_merge_v68/`; graphs, QA, complete model/tool traces and provider usage are not committed.

## Controlled local repair

The frozen baseline and latest implementation received the same reviewed candidate, same exact-provenance feedback, model and configuration. The feedback repairs an original relation's correspondence to its surviving edge, preserving both `QA` and `qa` SourceReferences.

| Metric | Frozen baseline | Latest implementation |
|---|---:|---:|
| Model calls | 7 | 4 |
| Tool calls, including structured submission/completion | 10 | 6 |
| Input tokens | 314,013 | 87,695 |
| Output tokens | 22,867 | 5,461 |
| Total reported tokens | 336,880 | 93,156 |

Final graphs are exactly equal, including all node/edge fields, sources and relation contracts: 122 nodes, 153 edges. Independent public-result provenance audits pass. Calls decrease 42.9%; total reported tokens decrease 72.35%. This is one real-model sample per version, not a statistical guarantee or a full semantic certification of the inherited candidate. The latest comparison uses execution code `4a1e2e7`; its artifact is `local_comparison_latest.json`.

Intermediate versions also completed in three calls. Those observations are retained but do not replace the latest-version comparison.

## Four-case integration and independent review

Initial whole-graph reconciliation completed in 32 forward and 37 reverse calls after restoring prior accepted work. Public provenance audits passed, but independent semantic review failed: an ordinary first-slot rule could bypass the saved conflict; field selection and output shape remained mixed; a branch reason asserted distinct people without evidence.

Explicit independent findings were then supplied to the local feedback interface. Final repairs use the same execution implementation, `4a1e2e7` (subsequent `48d730d` only documents it).

| Final local repair | Model calls | Input tokens | Output tokens | Nodes / edges | Provenance audit | Independent semantic review |
|---|---:|---:|---:|---|---|---|
| Forward | 6 | 269,857 | 40,998 | 123 / 154 | pass | pass |
| Reverse | 5 | 192,262 | 57,197 | 123 / 153 | pass | pass |

Both final graphs satisfy the reviewed goals:

- School administrator owns administrator email; chartered school owns charter number.
- Complete all-three-slot versus first-slot administrator-name alternatives retain exact case36/case63 references and applicability. No ordinary copy bypasses branch choice.
- Name layout consumes the selected operands and works with both alternatives.
- Singular email-slot selection is independent of top-K; plural field selection is independent of row/column shape.
- Unsupported business-priority and people-distinctness claims are corrected.

The final graphs differ in predicate/intent names and branch applicability representation. The reviewer found the stated goals semantically consistent, not byte-for-byte identical. One graph retains common applicability plus branch-specific applicability; optional normalization remains a representation difference.

These are independently repaired final results from different development/resume trajectories. They **do not prove** that a fresh, frozen-code, unassisted merge is order-independent. The 6/5 calls are local repairs, **not full integration costs**.

## Failures and total development cost

A reverse local attempt exhausted ten calls on numeric ID collisions with unseen physical objects: 568,154 input and 35,864 output tokens. Supplying a fresh per-batch ID namespace completed the same-candidate/same-feedback repair in five calls, with semantic and provenance review passing.

An earlier forward local process started before the impact-scope correction and exhausted 25 calls on repeated reconciliation. The successful rerun used corrected scope and explicitly included the direct operands. Because scope and implementation both changed, this is not a single-factor cost comparison.

Final ancestry chains, including accepted prior work, failures and repeated reviews:

| Chain | Recorded calls | Reported input tokens | Reported output tokens | Calls missing usage |
|---|---:|---:|---:|---:|
| Forward | 136 | 6,590,454 | 665,450 | 1 |
| Reverse | 138 | 5,353,141 | 757,705 | 1 |

All v68 experiments, including abandoned branches, earlier diagnostics and controlled comparisons, total **601 unique model invocations**, with **24,718,914 input + 2,934,134 output tokens reported**. Trace copies are deduplicated by invocation UUID. Three failed requests lack provider usage; no token or monetary cost is invented for them. SDK-internal transport retries may lack separate usage.

Local repair savings are demonstrated. **End-to-end production cost reduction is not demonstrated.** Repeated full reconciliation on recovery and large indivisible source/conflict groups remain significant costs. Completed-workspace feedback now reopens only named targets and outside impacts, but ordinary interrupted-run resume still restarts reconciliation.

## Verification

The complete suite passed 1,084 tests with one existing Pydantic deprecation warning. After the final ID namespace/diagnostic adjustment, 14 relevant engine/batch tests passed. Production decisions contain no database-, column-, case- or topic-specific deterministic semantic rules. Experimental feedback names actual object IDs; it is not embedded in production behavior.
