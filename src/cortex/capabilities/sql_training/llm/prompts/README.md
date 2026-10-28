# Extraction prompts

One Learning Agent owns the case's Working graph. Type-specific guidance is separate from the startup prompt and loaded only when needed.

| File | When the model sees it | Responsibility |
| --- | --- | --- |
| learning/direct.md or learning/repair.md | Every model call, after the graph model | Stage-specific extraction or repair steps |
| learning/system.md | Every model call | Task purpose, two-layer graph model, stage slot, on-demand guidance and delivery semantics |
| learning/types/concept.md | Concept guide requested | Definition, targets, Value materialization and one complete node example |
| learning/types/metric.md | Metric guide requested | Formula, refs, pattern, plans and one complete node example |
| learning/types/generation_rule.md | Rule guide requested | Condition/action/reason, triggers and one complete node example |
| learning/assessment.md | Independent Assessment | Review the exact candidate and evidence |

## Input and output

`learning/inputs.py:system_prompt` fills the system Markdown stage slot with Direct or Repair steps. The model reads task background → graph model → stage steps → on-demand guidance → delivery. Tool parameters and output protocol are supplied by native tools, not copied into the startup prompt. Dynamic Question, Gold, evidence, Working revision, semantic summaries, relevant physical bindings, feedback and notes remain in a refreshed independent case context. The startup prompt contains no full properties schemas, relation inventory or operation registry.

The case source is a whitelist of question/sql/evidence, not the internal training record. There is no graph_info block. Gold bindings contain relevant table IDs/names and column IDs, owners, SQL names, types and nonempty descriptions; samples and other details are read on demand. The semantic index contains at most 20 relevant ID/type/name entries, plus tentative or episode-group members; physical anchors and name/alias matches determine relevance. It is a discovery aid, not proof that unlisted knowledge is absent. Repair alone receives nonempty targets/feedback; empty notes and unused baseline revision are omitted. Internal metadata remains available to Diagnosis and the controller.

The deliverable is complete semantic nodes passed directly in `edit_graph.upserts`, then `handoff`; it is not another JSON response in assistant prose. Native LangChain signatures own tool parameters. The default `inspect_graph(mode="guide", node_type=...)` returns usable type guidance and an example, the common envelope, that type's exact Pydantic properties schema, and its relevant refs-derived relations in one response. Only Rule gets the compact registry, in `direct_rule_registry`; reading its guide is the required Direct authoring step, with no separate registry tool call. Detailed domain exceptions remain available through `guide_section="contract"` or `contract_ref`, and registry examples through `guide_section="taxonomy"`.

## Current graph capabilities

All Tables and Columns are initialized. Before creating a Value, inspect its owner Column with `inspect_graph(mode="node")`, follow incoming Value relations and read candidates to compare the exact owner, scalar and scalar type. Reuse a matching ID; omission from case context is not proof of absence. Only confirmed missing Values need proposals. Use `edit_graph.values` with an exact Column ID and stored scalar; code verifies the database, assigns the shared canonical Value identity and returns `value_ids`. Concept predicates can use the same automatic materialization path. `inspect_graph(mode="guide", node_type="dimension_value")` explains this contract.

Explicit edges use the same atomic `edit_graph` through `edge_operations`, including create/update/delete. `inspect_graph(mode="relations", edge_type=...)` supplies the exact relation constraints. Explicit edges, Values and semantic nodes all enter version fingerprints, graph diffs, Assessment groups, filtering, rollback and recovery. GraphManager generates default edges from refs and validates explicit relations in the combined graph; an explicit edge on the same anchor overrides that default, and deleting the override restores the default while the anchor remains. Independent facts do not alter formula/binding refs.

Each type guide exposes its refs field, target types and default relation names from the same mapping used by the graph compiler. Concept grounding carries the target-local ID. Rule guides always include `direct_rule_registry`: Direct must choose its `operation_intent` there; taxonomy is only for additional examples. No separate registry call is required.

## Maintenance and checks

Assessment uses the task definition in learning/assessment.md plus the generated schema from training/assessment_models.py. It reports unit quality, application aggregated across all attempts, per-target resolution across all attempts, regressions and specific Learning feedback independently; no model-authored group verdict is supported. training/assessment_inputs.py co-locates each changed node/relation and its accepted predecessor, supplies related knowledge and physical metadata, and computes per-attempt raw retrieval, selection, actual context presence and dependency inclusion facts using the nodes/edges actually passed to generation. Global raw ID lists are omitted after these facts are computed. Code validates coverage and attribution, combines unit eligibility with group/coupling constraints, and still requires matching stable SQL verification before acceptance. SQL evidence and prior comparison evidence are reused without extra validation calls. Repair receives independent results, deterministic stage facts and gap/preserve feedback. Prompt/schema changes invalidate old checkpoints through the assessment policy fingerprint.

- Edit the phase file for the task, system.md for common essentials, and one type file for its extraction guidance. There is no separate Shared extraction rules layer.
- Model schemas own field shape; GraphManager and the existing type validators own graph legality. Never hand-copy their schemas into Markdown.
- Keep each paragraph and list item on one physical line. Preserve structural newlines in headings, lists and fenced JSON examples.
- Test every complete example through the actual edit tool. Check that a guide is usable without a second evidence read and stays within the default tool-result budget.
- Recovery identity includes startup prompts, the rendered semantic/Value/relation guides, schema contracts and configuration. Restart the process after changing cached prompt files; incompatible checkpoints are rejected.
- Direct newly added Question triggers must be copied from a contiguous source phrase (case/whitespace normalized); accepted historical phrases are retained, and Repair is exempt. This provenance check atomically rejects invalid batches. Measure first-call JSON/argument validity separately from final accepted legality. Evaluate semantic precision, recall and important field correctness against human expectations; SQL correctness alone is insufficient.

Design references: [OpenAI skills and on-demand instructions](https://developers.openai.com/api/docs/guides/tools-skills), [focused agents](https://developers.openai.com/api/docs/guides/agents/define-agents), [task-specific evaluations](https://developers.openai.com/api/docs/guides/evaluation-best-practices).

Concept and Metric definitions are authored once in `asset_contract.CONCEPT_DEFINITION` and `asset_contract.METRIC_ATOMICITY`, then rendered into their guides and Assessment. Detailed contract sections provide concise binding, formula and Rule conventions. Keep dataset-specific decisions in case evidence, not permanent instructions. Direct registry/taxonomy excludes joinability; ordinary joins use existing edges. Repair retains observed-error join guidance and column-choice correction Rules even when the correct Concept binding exists. References capture direct necessities rather than the whole reachable graph.


Generation Rule atomicity is authored once in `asset_contract.RULE_ATOMICITY` and shared with Assessment. One intent may require multiple inseparable operations; the registry permits discovery rather than requiring an asset for every SQL operation. Keep unrelated outputs and current request values out of condition/action.

Optional `database.description_path` loads original column metadata through the existing DatabaseDescriptionLoader. Catalog comments remain in Column description; external source fields remain in source_metadata. Relevant case bindings disclose business names and short descriptions; full text and value encodings are available through the existing node read. Never expand abbreviations without supporting metadata.

Concept sql_expression is executable qualified SQL, not a graph ID or plan token. The existing normalizer and validator parse projection expressions consistently; serialization removes unnecessary identifier quotes and retains required quoting and literal values. References stay canonical node IDs. Aliases are unchanged by this update.


Assessment input v4 keeps refs and automatic edges together, with edges projected to id/type/from/to and semantic properties. Equal context facts are grouped by attempt_ids with exact coverage validation. Every SQL is retained; successful result samples are omitted and failed comparisons keep differences. Physical metadata keeps source meanings and only supplies value encodings for relevant Value/predicate review. Node definitions are not truncated; empty presentation fields are omitted without changing stored nodes. The output schema and SQL-plus-Assessment gate remain unchanged; incompatible checkpoints are rejected by policy v4.
