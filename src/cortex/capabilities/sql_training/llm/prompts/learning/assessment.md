# Task

Independently assess (1) ontology quality, (2) recorded injection into the SQL Agent, and (3) actual related SQL errors. Do not edit the graph, invent targets or authorize acceptance. Do not judge whether every unit was used by SQL.

{shared_extraction_contract}

# Inputs

units is the exact review inventory: candidate, optional accepted_before, quality_requirements and context_facts. quality_rubric supplies the checks. Read related_knowledge and supplied relation contracts with the units. physical_context contains relevant endpoints, not the whole catalog; scoped omissions do not establish database-wide absence. Historical definitions and round_baseline are comparison evidence, not current assertions.

attempts contains the current SQL evidence. fixed_targets defines existing repair obligations; prior_evidence supports regression comparisons. change_groups controls joint retention. Code owns injection observations and the derived quality summary.

# Evaluation

## 1. Content and consistency

Judge exactly the supplied quality_requirements. Inspect definitions, bindings, formulas, operands, business meaning and relation contracts. A missing contract cannot have valid predicate_precision. For minimality defects identify independently meaningful units, their distinct matches and the composition. For conflicting or redundant assertions identify the actual claims and scope. Preserve necessary direct operands and assertions needed by consumers; structural patterns alone are not defects. Use inconclusive only for a specific material gap that prevents a mandatory judgment.

SQL success does not excuse a defective ontology definition. Conversely, an unused supported node, an unselected endpoint or a missing preferred SQL spelling is not a quality defect. Check causal explanations against the evidence instead of treating Diagnosis as an axiom.

## 2. Injection

Actual context presence establishes injection and overrides incomplete selection tracing; explicit absence overrides selection. A recorded dependency path can establish inclusion without direct selection. Raw recall alone is not injection. Read these code-recorded facts; do not output retrieval judgments or demand that an injected unit be used. Do not invent missing paths or change correct SQL to exercise knowledge.

## 3. Related SQL errors

Inspect actual SQL behavior: an unresolved target due to inaccurate/incomplete knowledge, a new related error, or a regression against prior_evidence. An unrelated SQL error does not condemn this unit. Equivalent implementations are allowed; result equality alone is not a general equivalence proof. Values may be used in CASE, projections, joins, comparisons and calculations, not only WHERE filters. SQL uses physical names, not ontology IDs.

For a unit's sql_error feedback cite its exact unit ID, affected current attempt_ids and a verbatim sql_excerpt. Explain the concrete error, the responsible knowledge gap, the necessary correction and behavior to preserve. Do not invent causality or report harmless non-use as an error.

Resolve each fixed target only when its required behavior is demonstrated across the supplied attempts. Report a remaining error without inventing new target IDs; the controller owns re-diagnosis. Report regression attribution as supported only with evidence connecting it to candidate units; otherwise retain uncertainty. Never treat uncertainty as a confirmed defect or combine different attempts into one successful behavior.

# Output

Return schema JSON covering every unit and target exactly once. quality_checks contains exactly quality_requirements; omit the derived quality summary. Unit feedback contains only concrete quality defects or related sql_error entries; return none when neither exists. Do not output application labels or code-owned retrieval_results.

Target feedback uses kind=target; regression feedback uses kind=regression. feedback.location is an exact supplied graph node/edge ID. target_results.target_id is a target ID, never a graph location. Preserve substantive defects during format correction. Give actionable feedback, without passing-item narratives or per-attempt repetition. Couple only semantically inseparable units. The controller decides acceptance from these conclusions, injection facts and SQL verification.
