# Extraction prompt/context compaction v76

## Scope

Learning and Assessment share `learning/shared_contract.md`, rendered once into each system prompt. This is the same semantic contract for both agents: NL minimality, business referents/identifier representations, matching groups, direct binding uniqueness, real calculation admission, atomic generation decisions, Event evidence, explicit relationship contracts and evidence-scoped conclusions. No new node/role fields, case-specific rules or global-merge obligations are introduced.

Legacy extended contracts remain available to other project modules. Extraction's short common contract replaces fixed full tutorials; per-type authoring guides supply implementation details and examples. Assessment receives only its applicable rubric entries and the saved contracts of relevant edges, without an extra tool-read stage. Existing quality checks, SQL attribution, response validation and acceptance gates remain unchanged.

## Context organization

- Fixed: concise type boundaries, shared ontology/evidence contract, agent duties and output protocol.
- Per task: current case, candidate and necessary neighbors, applicable quality requirements, SQL attempts, and actual targets/prior evidence.
- On demand for Learning: type authoring schemas/examples, complete predicate contract and operation registry when authoring a Rule. Guidance already in context should be reused.
- Relation index reads are lightweight; exact predicate reads include the edge-write guide/schema. Pagination applies to useful index contents rather than repeating a full guide on every page.
- Model-view compaction removes identical common relation-guide fields only; contracts, relation rows, pagination and changed schemas remain. Raw history remains intact, and the latest complete guide survives trimming.

## Provider evidence

DeepSeek response parsing retains returned reasoning_content even if thinking was requested disabled, so unexpected backend behavior is observable. When explicitly disabled, captured reasoning is not resent as conversation context. This changes observability, not the requested model mode. Request parameters and raw provider responses are recorded in the local experiment runner; credentials/headers and .env are excluded.

## Validation and cost reporting

Use offline tests for contract equality, full index pagination, preservation of distinct relation facts, changed-schema handling, valid example writes and response parsing. These do not establish semantic model quality.

Real evaluation uses a controlled Assessment on the prior Case36 candidate and SQL evidence, followed by cold full extraction for Cases36 and63. Model deepseek-v4-pro and the current user-configured endpoint are fixed in manifests. Inner/outer rounds are5; there is no model-call/episode cap. Three consecutive correct SQL replays remain required. Log all calls, failures and reported usage; distinguish controlled replay from complete accepted case results. Older incomplete runs are not equivalent full-pipeline baselines. No case-specific correction feedback is supplied.

The original baseline snapshot was pushed as1f06fbe on codex/incremental-merge-sessions. Runtime results are recorded under output/extraction_compact_v76 and are not committed with code or credentials.

## Observed results and recovery

149 focused regression tests pass. The same Case36 Assessment candidate/evidence produced exactly the same17 unit conclusions after compaction: reported usage36,992 ->32,545 tokens (−12.02%); input15,424 ->11,940 (−22.59%). Latency264s -> approximately268s did not improve. The short common standard increases Learning's initial system message because it now includes the same core semantics as Assessment; type tutorials/repeated relation guides decrease. Do not infer a total pipeline saving from one message length.

The raw service response contained86,467 characters of reasoning_content despite explicit thinking.disabled and reasoning_effort=none. This explains a major observable source of generated content, but does not establish the gateway's internal routing or exact tokenization. Ordinary response parsing previously hid this field.

Case63 completed with3 consecutive correct SQL replays, independent Assessment and stable_correct, retaining6 Concepts and1 output-order Rule. Its accepted lifecycle used30 calls and568,515 reported tokens, including an Assessment timeout/recovery.

Case36 has NOT been accepted. Its first lifecycle generated a Rule bundling ranking with administrator field selection; Assessment correctly rejected atomicity. Two SQL attempts returned the required6 columns, but a third concatenated them into3 columns, so it did NOT achieve3 consecutive correct replays. Do not infer stability from the number of generation calls. This remains visible in the unaccepted draft.

The Assessment also used target ID E1 as feedback.location. The old vague format error did not identify which feedback item was wrong, and the model repeated the invalid response. v76.1 changes only diagnostics: name the owner/path, invalid ID and supplied node/relation IDs, preserving the same validation/acceptance contract. A controlled real format retry corrected the location in1 call (~30s), preserving both substantive defects. No target ID is accepted as a graph-node location and no verdict is normalized in code.

Because changing validator source changes the checkpoint policy fingerprint, Case36 v76.1 restarted cold instead of bypassing compatibility. That run was interrupted by a connection error on its second request. There were also a502 and two300s client timeouts in prior attempts. Network-only recovery used900s waiting without changing the5x5 rounds or introducing a call cap. Case36 remains blocked by the configured service, with no accepted graph claimed.

All attempts, including controls, used65 actual HTTP/model requests:933,535 input +237,709 output =1,171,244 reported tokens. Four failed requests lacked usage, so this is not a complete bill. Controls account for2 calls/50,028 tokens; full-pipeline attempts account for63 calls/1,121,216 reported tokens. No end-to-end cost reduction has been established. Do not compare incomplete old runs to complete accepted lifecycles as if they were equivalent A/B trials.

Artifacts: output/extraction_compact_v76/{index.html,cost_audit.json,quality_review.json}. A local review exporter revision-pointer bug was repaired without model calls; it did not change workflow verdicts. Case63 names are represented by separate first/last-name Concepts, whereas Case36's draft represents the Administrator through name groups; this modeling choice is exposed for review. These two cases do not establish real-model coverage for Metric/Event.
