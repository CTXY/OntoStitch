> 2026-10-25 更新：运行时现已统一为官方文件型 SQLite / SqliteSaver，移除 PostgreSQL 依赖。下文涉及旧 PostgreSQL 决策的内容仅保留为设计历史。

# Transactional ontology merge workflow

The merge operator integrates incoming case definitions into an isolated working copy of `existing`. Empty graphs receive new nodes; matched existing semantic nodes keep their canonical IDs. Original inputs and source evidence remain fixed, separately readable material. This change adds no Concept or target fields.

## Ownership

GraphManager checks schema, declared references, endpoint types, conflict envelopes, derived relationship consistency and declared graph invariants. It does not judge business identity, formula correctness or whether multiple bindings are interchangeable. A Concept target may reference multiple physical tables. Its SQL expression remains the complete binding; its references and `grounded_on` edges are not a replacement execution program.

Matching, Consolidation and independent Assessment own semantic judgments. A stored normal node is not a proof of business correctness; `applied` is a transaction result, not training acceptance. Database SQL replay remains an external check.

## Workflow and checkpoints

The parent LangGraph is `match_nodes → prepare_graph → integrate_group* → finalize_graph`. Its checkpoint contains original request/schema/workflow fingerprint, fixed source evidence, identity groups, current working GraphState, pending groups, mappings, completed transaction revisions and diagnostics. Each group has the existing `prepare → consolidate → assess → finalize` LangGraph and bounded LangChain agents.

Physical identities are processed Table, Column, DimensionValue, with owner mappings applied before matching dependent identities. Physical changes are submitted together so forward references remain complete. Semantic groups are scheduled after their pending dependencies, including dependencies already present but awaiting update in this run. Consumers see newly accepted definitions and conflict alternatives. Existing-only unrelated definitions are retained without semantic model calls.

Within a group the base graph is fixed. The agent reads current existing nodes, original inputs and candidate previews using `read_nodes(view="existing"|"input"|"candidate")`, with outgoing relationships and incoming consumers. Evidence reads are bounded and unavailable material is explicit. Candidate graphs are visible to Assessment only.

Consolidation returns complete NodeContent candidates, source assignments, difference analysis, confirmed conflict points and unresolved originals. It does not submit arbitrary CRUD or allocate canonical IDs. An assessed `identity_mismatch` is workflow metadata: the controller separates original members and leaves existing nodes intact. It is not a new ontology property. It must preserve every original, must not contain constructed candidates, and requires independent source coverage.

## Matching

- Concept identity is the same business meaning, interpreted using names, description, complete bindings and evidence. A shared physical binding alone does not prove identity.
- Metric identity is the same business quantity; formulas and complete plans explain its subject, grain and behavior. Competing formulas can belong to one identity.
- Rule identity compares requested result, governed objects/roles, independent obligation, and whether differences change the request or only its implementation. Shared SQL syntax is insufficient. `operation_intent` is a retrieval clue, not an exclusion rule.

Candidate retrieval uses names/aliases and implementation material; Rule retrieval uses condition/action similarity and optional embeddings. Matching receives bounded question/SQL evidence. Inconclusive matching retains separate nodes. Cross-type Concept/Metric replacement is not enabled by the production builder and the transactional operator rejects unreviewed cross-type replacement instructions.

## Candidate transactions

`transactions.py` compiles a candidate snapshot into ordinary GraphManager operations. Preview executes these operations on a GraphManager copy. Structure/reference failures return code/path/hint feedback to Consolidation. Assessment then checks generalization, source coverage and graph effects. Actual working-graph updates use the same compiler and base revision. No GraphManager commit callback is called by group integration; the existing application layer publishes the final graph through repository CAS.

The compiler submits create/update/set_conflict and explicit relationship changes as one batch. GraphManager maintains derived edges. `reviewed_reconciliation=True` is a trusted service argument, not an Agent operation field. It permits a reviewed complete normal definition to replace an existing conflict under the same ID/type, preserving sources supplied by the accepted result. Ordinary CRUD remains unable to overwrite unresolved alternatives.

When reviewed reconciliation removes a conflict envelope, default branch edges are rebuilt and explicit branch edges lose obsolete alternative scoping. If new content invalidates an explicit anchor, implicit loss is rejected; callers must explicitly reconcile the relationship. Relationship-only changes on conflict owners include an unchanged `set_conflict` operation in the same transaction. Rejected transactions leave the graph unchanged.

The preview explicitly lists branch-to-unconditional relationship scope changes. Assessment must approve every supplied edge ID through its workflow-only `approved_relation_scope_changes` result; unknown IDs, missing approvals or unsupported expansion cannot accept the candidate. This is semantic review metadata, not a new Concept/target field or a business decision inside GraphManager.

## Conflicts and preservation

Complete competing definitions are compacted into common fields and whole-field overrides. Every alternatives owner remains `merge_status=conflict`; confirmed competition is distinguished from `consolidation_status=unresolved`. Candidate dependencies retain `alternative_ids`; Concept bindings retain `target_id`. Existing alternatives and sources are restored for subsequent consolidation rather than treating common content as a default definition.

Different Concept targets are not automatically equivalent. External conditional target selection has no new schema: when current content cannot preserve the choice, keep complete unresolved alternatives. Metric formulas/plans and Rule condition/action may express supported conditional behavior, but analysis alone does not preserve it. Missing evidence, rejected generalization or exhausted budgets preserve originals. Conflicting dependencies trigger consumer assessment rather than mechanically marking every consumer conflict.

Target duplicate checks preserve SQL literals and identifier case. The target normalizer performs limited representation/ID normalization; it is not an equivalence prover. Edge mapping retains explicit variant mappings and does not invent a mapping when multiple final variants are ambiguous.

## Observability and provider compatibility

LangSmith uses the existing payload-hidden native tracing context, preserving model usage and metadata. Graph and group spans expose run/group/base-revision identities. `merge_transaction` spans include transaction ID, base revision, result revision and status. Local audit sinks retain proposal/review/stop reasons. Source SQL and ontology payloads remain hidden from the native summary client.

LangChain ToolStrategy remains the structured-output mechanism. A small model adapter requests automatic tool choice because reasoning providers can reject forced `any/required` tool choice. Structured output still must validate; free-text output does not silently pass. Call limits, independent assessment and preservation apply on failure. Production defaults to the official Postgres checkpointer; the review script explicitly injects the official SQLite saver for reproducible local runs.

The bounded after-model middleware requests the structured tool again when a response is plain text or contains invalid JSON tool arguments, including the actual parsing error. It does not silently repair or accept malformed arguments. The model-call limit middleware runs before the retry jump so these retries count toward the same budget.

## Validation and local review

Tests cover multi-table targets, literal-safe deduplication, schema rejection rollback, conflict-to-normal/normal-to-conflict updates, explicit branch relationships, candidate previews, feedback-driven revision, identity correction, physical owner identity, dependency ordering and parent/agent checkpoint recovery.

Run `python -m pytest -q` from the repository root. `scripts/review_merge_cases.py --live --run-id <id> --output <new-directory>` runs the prepared five cases with evidence, SQLite checkpoints, local role traces, graph/report JSON and an HTML view. Reuse the same run ID and unchanged inputs to resume; use a fresh output/run after workflow or prompt changes. The script does not execute SQL replay.
