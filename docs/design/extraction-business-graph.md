# Extraction training: case-level business graphs

> Historical implementation and trial record. The 2026-11-03 design discussion supersedes its optional Concept bindings, stored-quantity interpretation, generic participant-role guidance and refs-derived semantic relations. See [the current design](ontology-graph-design.md) and [implementation proposal](ontology-graph-implementation-plan.md). The recorded 81/84 review and 811-test result are historical; they do not validate the revised semantic requirements.

This change extends case authoring and assessment. Global graph revision and metadata learning are outside its scope.

## Shared node contract

The physical catalog remains Table, Column and governed DimensionValue. Agent-authored semantic types are Concept, Event, Metric and GenerationRule. Node types share one versioned root schema across Direct and Repair. An episode chooses supported knowledge; there is no required type coverage or output count. No Entity or Process node type is introduced.

- **Concept** defines an object type, category, attribute or state. Targets are optional; an unbound Concept requires a business definition.
- **Event** defines a business occurrence type, not an individual record. A nonempty description is required. Optional targets contain complete boolean occurrence-identification predicates. Timestamp/ID/amount scalars, request-date specializations and unrelated field presence do not establish a whole occurrence binding. A target does not prove event countability or one-event-per-row grain.
- **Metric** preserves one complete named quantity, including intrinsic normalization, operand, NULL behavior and physical implementation. Name, definition, formula and pattern must agree.
- **GenerationRule** preserves one reusable SQL-generation decision, including its supported condition, action, reason, direct operands and operation label.

The NodeAsset models, formal GraphNode content checks and GraphManager share Event predicate validation. Existing snapshots have schema fingerprints; incompatible old checkpoints require regeneration rather than silent conversion.

## Explicit business relations

The root adds has_participant, measures, concerns_event, part_of, has_stage and precedes; broader_than also supports Event classification. Their endpoint rules are defined in the root schema. Participant roles and relation conditions participate in relationship identity. Business explanations are stored in properties.description; conditions limit the asserted fact.

Business relations are authored through edge_operations. Classification, composition, participation and metric context are never inferred from arbitrary refs. Existing physical/formula anchor compilation remains: target references maintain grounded_on(target_id), formula references maintain dependencies, and rule references may target Events through applies_to. This preserves the current snapshot and binding lifecycle; it is not a full replacement of the shared reference compiler.

Custom predicates remain available. Their descriptions must make the relationship interpretable; names alone grant no JOIN execution, transitivity, inheritance or constraint-checking behavior. An Agent cannot write source, reference_origin or alternative_ids metadata.

Composition checks normalize has_stage(parent,child) to part_of(child,parent) before cycle detection. Business adjacency is not globally treated as a calculation DAG.

## Agent workflow and progressive disclosure

1. Direct receives the original case, relevant physical bindings, a bounded semantic index and available semantic types.
2. inspect_graph(mode="guide", node_type=...) loads one Markdown guide, exact authoring schema and valid example. The GenerationRule guide includes the operation registry for both stages.
   Guide schemas omit redundant annotation text while retaining validation constraints, property names and literal enum/const values. Agents load guidance for the next coherent edit instead of repeatedly collecting every type guide.
3. inspect_graph(mode="relations") provides a paged predicate index; edge_type retrieves exact endpoint/property/identity requirements. Duplicate inventories are omitted to preserve the tool reply budget.
4. edit_graph writes coherent nodes, verified values and explicit relations atomically. Exact catalog IDs and SQL names remain different concepts.
5. handoff explicitly delivers the tentative revision. Optional direct_type_coverage records considered types; it is not an acceptance quota.
6. SQL replay, Diagnosis, Repair and independent Assessment determine which changes are accepted. Repair can modify any semantic type or relation, including assessment-only corrections when SQL is already correct.

All tool arguments reject unknown fields. A new rejected write counts as actionable feedback for stagnation tracking, allowing a correction after repeated reads. Consecutive-error, model-call and time budgets still bound the episode. Neither budget exhaustion nor successful SQL fabricates a completed handoff.

## Operation registry

The GenerationRule guide exposes a compact full operation_registry plus Direct discovery labels. Repair sees existing operations and the added column_binding_choice, aggregation_grain, null_semantics, temporal_alignment and population_scope labels. Detailed alternatives are loaded on demand through taxonomy guidance. Labels aid retrieval and review; they do not establish semantic correctness. Registry extensions are code-defined and validated for unique, nonempty intent names and descriptions.

## Assessment and acceptance

Each change unit receives a code-generated application_requirement:

- **sql**: executable bound Concepts, Metrics, GenerationRules, governed Values and dependency edges require SQL application evidence.
- **semantic**: Events, unbound Concepts and independent business relations require supported content and structure; absence of new SQL syntax is not a defect by itself. Event physical bindings are still checked for fidelity.
- **none**: deleted units require removal-safety review.

The assessor cannot assign its own application_requirement. Quality, local application, fixed-target resolution and regression conclusions remain separate. Invalid or inconclusive semantic knowledge cannot pass by choosing not_applicable. Stable SQL replay remains a separate controller gate, and an incomplete Direct episode is needs_review even when the physical catalog alone yields correct SQL.

Assessment inspects intrinsic versus request-specific scope, participant roles, complete event bindings, quantity versus population composition, exact metric arithmetic and the supported cardinality of generation rules. SQL equality on the current database cannot prove business relationships or unrestricted generalizations.

## Scope of downstream changes

Training retrieval recognizes Events as semantic assets in recall, selection and result-contract validation, using the existing semantic recall budget. Raw GraphSpace SQL validation reads Event definitions and relations. This is the minimum integration needed to exercise extraction; global merge algorithms and full online event/process reasoning are not redesigned here.

## Trial evidence

The live review uses BIRD dev questions 47, 49, 60, 76 and 77 from california_schools, with original database-description files. School opening supplies positive occurrence evidence; the other questions exercise closure state/time, composite categories, grade bounds, and metric population scope. Neither a positive-event label nor the absence of an Event replaces independent semantic review. A supplemental financial question 104 checks transaction occurrence, account participation and the distinction between transaction date and account-opening date without description files.

The experiment configuration explicitly disables embeddings after the configured endpoint returned HTTP 404, and uses existing lexical recall plus LLM selection. SQL generation, execution, two consecutive correct replays and Assessment remain enabled. No custom generated metadata store is used or updated.

Review artifacts retain failed rounds and independent findings. Final review packets include original questions/evidence/Gold, accepted graphs, SQL replay facts, explanations and independent scores. This is a selected-case smoke study, not a BIRD-wide performance claim.

## Recorded verification: 2026-11-03

The final selected artifacts passed independent subagent review (81/84). The reviewer used seven 0–2 dimensions: structural validity, evidence precision, business coverage, binding fidelity, event restraint, relationship usefulness, and rule scope/registry quality. Acceptance requires no critical defect, a score of 2 for structure/evidence/bindings/event restraint, and at least 12/14 overall. SQL execution is checked separately.

| BIRD database / question | Selected source round | Semantic nodes | Independent semantic-to-semantic relations | Review |
| --- | --- | ---: | ---: | ---: |
| california_schools / 47 | R6 | 8 | 5 | 14/14 |
| california_schools / 49 | R5 | 10 | 3 | 13/14 |
| california_schools / 60 | R3 | 9 | 6 | 13/14 |
| california_schools / 76 | R4 | 9 | 8 | 14/14 |
| california_schools / 77 | R3 | 8 | 2 | 13/14 |
| financial / 104 | R2 | 5 | 4 | 14/14 |

The relation counts above intentionally omit physical grounding, anchor overrides and implementation constraints. The complete graph retains them. School opening and financial transaction contain actual accepted Events; the other cases need no Event quota. Every selected output passed two consecutive SQL replays.

These are the accepted outputs of iterative exploratory runs, not six fixed-version independent benchmark trials. Failed rounds, prompt changes and source rounds remain recorded. The final packet is under `output/extraction_schema_review_20261103/review/`; it contains a navigable HTML index, semantic projections, full graphs, original cases, exact accepted outputs, the independent rubric/review and a SHA256 equality audit. No graphs were manually patched to obtain the review scores.

Actual assessment-only corrections include:

- Financial 104: replace the opening-date/account `part_of` assertion with `attribute_of`, retaining the original date binding and SQL result.
- Schools 76: remove unsupported abbreviation expansions and correct the description of grade-span representations, retaining the accepted attributes and exact filters.
- Schools 47: distinguish nullable school-name labels from identity and narrow count-rule scope, preserving the exact Gold counted operand.

The final full test run passed 811 tests. Targeted lint and diff whitespace checks also passed. Remaining non-blocking review notes are redundant GenerationRules in simple lookups, DISTINCT wording precision and an optional metric business-scope edge. These remain visible for human review rather than being represented as perfect ontology recovery.

## Database-level physical catalog preflight

The offline supervisor builds and validates one physical catalog before starting case workers. A schema-only inspection first resolves actual identifiers and validates the complete physical graph, without row sampling or LLM calls. Row sampling and metadata attachment then run once; a manifest is published only after the resulting graph passes validation. A failure prevents case dispatch for that database.

Workers use the existing `prepared_workspace` path, copying physical assets into their isolated workspaces. Case-specific value discovery, Gold JOIN evidence, semantic extraction, SQL replay and assessment remain local to each case. Reuse does not mark cases accepted or combine their sources. Prepared-catalog validation inspects schema without repeating row sampling. The shared manifest fingerprints database identity/scope, SQLite file size and modification time, descriptions, configured dimension values, root schema and asset files; mismatches stop reuse instead of silently running with stale or altered assets.

Physical `joins_with` may represent a declared self-reference: the table endpoints can be identical, while column pairs express the two roles. Column existence/ownership, full equality keys, source preservation and constrained cardinality still apply. This is independent of business hierarchy acyclicity. A reversed self-reference reverses the entire composite key and its cardinality, not individual column pairs independently. Cross-table path search remains bounded and does not traverse self-loops as new tables.

SQLite foreign-key table and column identifiers resolve to stored spelling using SQLite's ASCII case-insensitive comparison. Persisted IDs retain that spelling; this does not impose lowercase IDs or SQLite comparison rules on other databases.

A fatal extraction report can legitimately contain no case results. The runner records its original errors and initialization stage without indexing `cases[0]`. Supervisor exit reporting retains existing diagnostic fields. No model retry can repair a structurally invalid physical catalog.
