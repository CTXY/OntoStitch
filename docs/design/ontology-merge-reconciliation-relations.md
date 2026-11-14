# Relation-first Graph Reconciliation

> 2026-11-14：本文件记录隔离原型的设计演进和实验边界，并非当前运行已接入证明。统一语义要求见 [ontology graph 规范](ontology-graph-design.md)，包括必要节点重构与共享概念组织；下文早期冻结/终止策略保留作历史记录，不覆盖统一规范。

## Status and purpose

Review specification with an isolated prototype. Production merge code remains at the restored baseline (`f3fc367`, merge implementation equivalent to `51562bd`). No matched-node changes or complete merge run are included.

Reconciliation organizes accepted semantic knowledge: business edges, hierarchy, attribute bearers, relation contracts and relation_metadata. It does not routinely re-optimize Concept bindings, Metric formulas or GenerationRule actions.

## Modification boundary

| Object | Default policy |
|---|---|
| Owned business edges | May change predicate, endpoints, direction and description; may merge, delete or replace with explicit source correspondence |
| Relation contracts | Reuse existing contracts; define a concrete new business predicate when justified; do not redefine installed meanings |
| relation_metadata | May update supported group statements and member refs consistently with edge changes |
| Node title / aliases | Optional, explicitly scoped, same business referent only |
| Concept description / definition | Scoped identity-preserving referent normalization allowed, with independent Assessment |
| targets / expression / Rule action, condition, intents / alternatives / sources / node ID and type | Frozen |
| Physical catalogue and unowned edges | Readonly |
| Create, delete, merge or split nodes | Explicit node-reconstruction exception; outside the default relation transaction |

relation_metadata is stored under Node.properties, but represents relationship organization; allowing it does not authorize other node-property changes.

## Naming and reconstruction exceptions

1. **Naming clarification:** title/aliases clarify the same already-established referent. Preserve definition, targets, formulas, behaviors, alternatives and sources. A name/identifier mapping may denote its business object; reading such a mapping does not require a separate Entity class.
2. **Identity-preserving referent normalization:** name/description/definition confuse a business referent with its naming/identifier representation. Within explicitly scoped ordinary Concepts, update these fields consistently without changing business identity, bindings, formulas, behaviors, branch content or sources. Provide a same-ID change explanation. This is permitted in the local relation task and does not automatically require a separate node-content task or new Person/Entity node. Code verifies protected fields; independent Assessment must judge identity equivalence.
3. **Actual reconstruction:** different business identities, missing nodes, merge or split require the corresponding node integration and source validation.

Only genuine identity/knowledge changes beyond this normalization trigger an exception. The relation writer reports `failure: node_reconstruction_required: ...`. Assessment uses existing `NodeReview.verdict=revisit_identity` and identifies the required node clarification/reconstruction. The explicit `node_reconstruction_required:` issue marker is also recognized for compatibility with saved reviews. This ends the relation retry; it is not an instruction to invent a storage predicate or move a relation to an incorrect bearer.

After a separately validated node task, return to the affected relation region. Do not reopen all unrelated graph checks. No node reconstruction is automatically executed by this prototype.

## Region and context

Reuse semantic-anchor selection and bounded hub handling from the semantic-region prototype. Hints recall an area, not individual repair tasks.

For the already selected region, skip a paid discovery step when enough information is available. Supply:

- Owned complete business edges and their exact SourceReferences.
- Endpoint names, existing meanings, grouped bindings and necessary readonly background.
- A frozen Rule directory, not full unchanged actions/reasons or an operation registry.
- Relevant installed contracts and a shared concise core contract on every request.
- Original relevant QA/SQL/evidence.
- Direct physical refs, selected-value columns and selected-column tables; no table-wide expansion or FK traversal.

Background reads do not grant writes. If a necessary endpoint or relation group is absent, request a concrete scope extension or stop; optional neighbors are not mandatory queries.

## Local transaction

1. Generate one complete current-relative `BatchResult` using existing operations.
2. Code rejects node changes outside permitted rendering fields, Rule/conflict writes, physical edits and unowned edge writes before evaluation. Normalization of description/definition requires name, description and definition together, with a same-ID change explanation.
3. Restricted title/normalization/metadata edits may omit frozen fields. A thin adapter copies those fields from the accepted node, rather than asking the model to regenerate them. This never infers business meaning.
4. Reuse `ActiveBatch`, GraphManager/schema validation and `validate_rewrite_sources`. Retired assertions need actual surviving assertions; source coverage is not source broadcasting.
5. Validate owned retained edges use concrete predicates. Update metadata in the same transaction; do not silently drop a broken group.
6. Assess the actual local delta independently. On acceptance, a production integration would commit graph and lineage together. This isolated prototype writes artifacts only.
7. Permit a bounded local retry for relation defects. Node reconstruction is terminal for this task. No automatic increase in rounds/budget or whole-graph rerun.

Each retry starts from the unchanged accepted baseline. No cumulative invalid-patch state is introduced.

## Assessment scope

Three layers:

1. **Code:** IDs, fields, endpoints, contracts, scope, frozen content, metadata references and exact source coverage.
2. **Local semantic review:** changed relation bearer/direction/predicate/description, supported group meaning and identity-preserving naming.
3. **Source support:** whether particular sources support particular successor assertions; group-level reference coverage alone does not prove this.

Current endpoint title/meaning must actually match its role. Allowing normalization does not silently normalize an unchanged node: if a node remains defined only as a literal value while the retained relation requires its bearer, request the permitted rendering update. Compare retained shortcuts with existing paths: storage wording is not a distinct business fact.

Unchanged node bodies are context, but concrete endpoint defects that invalidate the relation are in scope. Frozen Rule actions and conflicts have no content-review targets; `decision_checks` and `rationale_checks` are empty. An endpoint defect fixable through identity-preserving normalization uses revise; only actual identity/reconstruction outside that permission triggers revisit_identity. It never justifies optimizing unrelated node content.

The reviewer must not accept a predicate that merely describes which record/table stores a field as a substitute for the required business relation.

## Avoid repeated checks

Coverage belongs to a specific judgment and its supporting facts. Source-choice checks depend on bindings, applicable Rule/branch content and evidence; an unrelated business edge update does not invalidate them. Structural checks depend on the actual assertions, endpoint meanings, contracts and relevant groups.

Naming-only changes preserve business identity and behavior. Actual node-content/reconstruction changes require checking affected consumers. Neither a new task ID nor optional frontier changes justify repeating the same accepted judgment.

## Isolated validation

Implementation and exact paid inputs/results:

`artifacts/merge_review/semantic_region_lab_v2/`

19 local tests cover field freezing, naming, existing transactions, exact sources, metadata invalidation and reconstruction routing. They do not prove model semantics.

The actual four-request official test is recorded separately from the post-run routing correction. It demonstrated scope/source enforcement, but the final proposal was **not accepted in the post-run semantic review**, despite the runtime Assessment returning accept. The prototype must not be promoted based on that verdict.


## Referent-normalization follow-up experiment

`artifacts/merge_review/semantic_region_lab_v3/` reuses the existing region and bounded runner. No complete merge or production update.

Four requests were recorded: one schema-rejected writer response, its corrected writer response, an Assessment that incorrectly accepted the candidate, and a same-input Assessment control with two general coherence criteria clarified. The control returned revise, correctly asking for explicit administrator-referent normalization and retirement of a storage-only shortcut.

No candidate was subsequently regenerated or accepted under the stronger review; the business goal remains unfulfilled. The actual final candidate contains the two-edge bearer path but retains the old name-string rendering and direct storage shortcut. Do not report the runtime acceptance as a successful ontology repair.

The control changed instructions only, not the candidate, evidence or supplied case feedback. One comparison does not establish general prompt reliability or end-to-end cost savings.


Assessment permissions must match Writer permissions even when no normalization was applied. `referent_normalization_ids` is the allowed set; `changed_node_ids` is the actual delta. Both are code-owned facts, not parallel LLM-maintained checklists. The follow-up found and locally corrected an intersection bug that erased permission for unchanged nodes; no additional paid verification was performed after that correction.

## Lightweight semantic input and shared-standard review

The v4 isolated input prototype (`artifacts/merge_review/semantic_region_lab_v4/`) addresses two separate omissions: detailed physical metadata distracting from semantic relationships, and the local prompts not reusing the existing ontology node definitions.

Initial input retains semantic IDs/types/names/aliases/meanings, exact grouped binding IDs with readable labels, explicit business assertions, supported group statements, source references and original case evidence. Do not duplicate these bindings as full physical catalogue entries containing types, nullable/key flags and external-file metadata. Necessary physical details remain separately available; omitted facts cannot be inferred from a field label. The isolated offline preview has no detail-read tool, so missing necessary evidence must be explicitly reported, not treated as complete knowledge.

Both roles receive the same semantic standard assembled directly from existing extraction `asset_contract.py` and review `quality_contract.py`, rather than another hand-maintained node taxonomy. Include Concept meaning/grounding, Metric calculation and NL minimality, GenerationRule decision/action boundaries, Event evidence threshold, business relation roles and existing support/consistency/nonredundancy criteria. Local scope and edit protocol remain separate: understanding a node type does not authorize rewriting its frozen content. Related saved contracts provide precise endpoint requirements; no full registry or authoring-schema dump is needed.

Assessment receives current candidate endpoint meanings once; only changed nodes need a before-version. All changed edges and unchanged owned comparison assertions remain visible. Include relevant installed, new and parent contracts, not only newly declared contracts. Schema/source checks remain code responsibilities; semantic correctness remains an independent Agent judgment.

This is an offline, same-input review prototype, not a production deployment or successful model repair. More complete guidance adds System text even when data shrinks; report both quantities and do not claim token savings without measuring actual calls. Historical paid messages and failure results are not overwritten.

## Tool-based partial editing experiment

The v5 isolated prototype separates the semantic reading view from the stored graph. Batch `inspect_items` reads selected node/edge detail before mutation; typed `submit_changes` accepts changed fields only. Code fills unchanged fields from the same accepted baseline and resolves short N/R/Q labels internally, reusing existing atomic transactions and exact-source validation. Tool parameter schemas describe writable fields; they do not replace the shared business ontology standard. Read requests never trigger Assessment; a mechanically valid complete local proposal does.

New descriptions/definitions can be submitted without repeating unchanged node fields. Code copying those fields does not prove coherent referent normalization. Business identity changes, node reconstruction and arbitrary frozen-property edits remain outside this local editing permission. Retirements require supported successors and explicit correspondence; the implementation never simply broadcasts all retired sources to every output.

Actual bounded official test: one batched read, one valid partial proposal, one independent revise review, one source-losing rejected revision, then HTTP 402. Five attempted/four completed requests; recorded 30,992 input and 12,660 output tokens. The administrator remained name-only, and review improperly treated SQL projection as a complete relation-evidence boundary. No semantic acceptance, complete merge or production update. The post-run single-field adapter improvement is locally tested only. Detailed traces and limitations are recorded in semantic_region_lab_v5/README.md.

## Evidence consistency, rather than missing-description rejection

The agreed semantic relation criterion is contradiction-based. A relation is not wrong merely because no case explicitly describes it or no SQL projects a particular bound field. Writer and Assessment use the full Question/SQL/evidence together, distinguish business roles from physical representations, and allow reasonable interpretation/generalization when supplied training evidence contains no counterexample.

For a proposed scoped fact f and supplied evidence E, a blocking semantic finding requires:

```text
SemanticReject(f) ⇒ ContractViolation(f)
                 ∨ ∃e∈E: SameApplicableScope(e,f) ∧ IncompatibleMeaning(e,f)
MissingExplicitDescription(f,E) ⇏ SemanticReject(f)
NotSelected(field,SQL) ⇏ Contradicts(BusinessRelationship(f),E)
```

These are review obligations, not a deterministic entailment or contradiction engine. The issue identifies the exact assertion plus the violated contract/role or an incompatible evidence fact. An ambiguous label or optional clarification alone is not a blocking issue. An actual mismatch between an endpoint's meaning and a predicate's role remains a contract defect; a name/identifier may represent its bearer, so do not force literal attribute interpretation from wording alone.

A question about a business role's attribute may express that role's relationship without returning its name. A contract requiring a representation in the source is not a requirement that every supporting query SELECT it. Existing compatible attribution is not removed for SQL omission alone. Reference preservation still does not prove semantic truth; new attribution needs a meaningful connection to the successor, and wrong facts are not legitimized by copying sources.

This is not a guarantee of universal truth or permission to silently strengthen type-level associations into mandatory participation, unique identities, cardinality, slot priority or exhaustive coverage. Those are separate assertions whose scope and meaning must be checked. Structural validation, exact provenance, alternatives, verified Value creation and protected node fields remain unchanged.

v6 keeps the same original subgraph, evidence and tools. One evidence-policy Markdown is inserted into the shared standard used by both roles, replacing the earlier explicit-support phrasing for this local task. Stage prompts distinguish actual contradictions from missing description and use the partial-field tool interface. It supplies no target topology or case-specific feedback.

Actual v6 local result: Assessment accepts the candidate retaining Q4 on the school–administrator relation; a detached reversed-email-edge control is rejected for explicit predicate direction/role violation. Five main requests plus one control request completed through the currently configured previously authorized ciyuanzhiyan.com service (deepseek-v4-flash), totaling 39,280 input and 18,479 output tokens. No full merge or production publication. The Writer left the administrator's full-name title/definition unchanged; relationship organization improved, but automatic referent normalization is not demonstrated. Detailed results and limits are in semantic_region_lab_v6/README.md.

## Upstream canonical-name content replay

Canonical name/referent/identifier guidance is now in extraction's shared Concept contract and referenced by production Node Consolidation/Node Assessment. The v7 local replay compares all original members of the three relevant already-confirmed identity groups from cases35/36/63/87, normalizes naming/definitions without changing bindings or Rule branches, independently assesses that node result, and hands the accepted graph to the same bounded local reconciliation. It does not rerun identity discovery, extraction or a complete merge.

Actual result: administrator naming/definition is normalized in the node step; the relation step leaves nodes unchanged and yields School → has_administrator → School administrator → has_contact_email → Email. Q4, exact provenance and the existing two complete conflict branches/branch edges survive serialization. Both reviews accept. Five completed model requests total 32,060 input and 10,960 output tokens (43,020), no retries. Original conflict behavior was preserved rather than newly constructed or semantically re-evaluated. Full scope/records are in semantic_region_lab_v7/README.md.
