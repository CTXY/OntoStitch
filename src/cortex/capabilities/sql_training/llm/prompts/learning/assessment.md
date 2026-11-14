# Task

Assess proposed ontology knowledge for one NLQ–Gold SQL case independently. Separate content quality, SQL application, fixed-target resolution and regression. Do not edit the graph, create new targets or authorize final acceptance.

{shared_extraction_contract}

# Inputs

units is the exact inventory. Each supplies candidate, optional accepted predecessor, quality_requirements, application_requirement and recorded context_facts. quality_rubric defines the applicable checks. Read related_knowledge and supplied relation contracts together with units; definitions need not be duplicated. Historical definitions are comparison evidence, not current assertions. A missing contract cannot have valid predicate_precision.

case and physical_context supply business/database evidence. attempts supplies all SQL verification for this candidate. context_facts establishes inclusion, not quality or application; actual presence overrides incomplete selection tracing, and explicit absence overrides selection. Do not invent inclusion paths. change_groups controls joint retention. round_baseline is an older comparison, not the accepted predecessor. fixed_targets and prior_evidence apply only when supplied.

# Evaluation

First inspect the supplied SQL attempts and their actual execution/result differences. Then compare the responsible knowledge and fixed targets. Function names or agreement with a Diagnosis explanation do not prove semantic application or error: verify the actual populations, groups, comparisons and NULL behavior. SQL patterns are illustrative unless their particular behavior is required; equivalent implementations remain valid. Result equality alone still does not prove equivalence beyond the observed data.

Diagnosis identifies observed failures; its causal explanation is not an axiom. If that explanation or a candidate Rule prohibits a valid implementation without evidence, report the unsupported restriction as a knowledge defect. Do not reinforce it by declaring the implementation wrong merely because the Rule or target says so.

## 1. Content and consistency

Evaluate exactly each unit's quality_requirements using the shared contract and rubric. Review Metric/Rule content before lengthy edge inventories; valid edges do not prove node quality.

For minimality failure give units, distinct field/value/operation matches and composition. For relation defects identify incompatible endpoint meaning, predicate contract or scope. Compare related Rules' shared decisions and overlapping conditions, including obligations embedded in compound actions; separate Gold support does not prove compatibility.

For redundant assertions identify equivalent facts or a declared rule and independent remaining premises. Preserve limits, consumers and direct operands; assess deletion against the final remaining graph, not mutually deleted witnesses. Report concrete defects, not missing-knowledge coverage or preferences for smaller graphs.

Assess optional relation_metadata under its owner's support/graph_consistency; no extra assessment object or SQL requirement. Use inconclusive only for a specific material gap preventing a mandatory check or interpretation of the actual claim, and identify the gap.

## 2. SQL application

Follow each unit's application_requirement. For sql, verify its candidate-owned behavior in every attempt. For semantic, supported relations/representation edges need no new SQL syntax. For none, check deletion safety.

For Concepts check request-relevant meaning, population and required matching behavior, not selection of every target or identifier. For Metrics/Rules verify the calculation/decision with required operands and supported grain/NULL behavior. Context inclusion alone is insufficient; equivalent SQL is allowed.

Return applied only for complete behavior and recorded inclusion in every attempt. Distinguish partial application, included-but-unimplemented behavior and missing attribution using the schema. not_applicable is allowed for deletions or semantic units without a current SQL obligation. An unrelated SQL error does not negate complete local application; whole-query equality does not prove it. Never combine behaviors from different attempts into one success.

Inspect every failed attempt's actual error or result difference. Do not dismiss a failure as merely a SQL-generator problem. If it prevents a unit's required output or behavior, that unit is not completely applied; identify the minimal actionable knowledge gap. A valid definition alone does not establish successful application. Preserve unrelated valid units and do not invent causality or business facts.

## 3. Targets and regression

Assess every supplied fixed target across all attempts; return no target conclusions when none exist. Whole-query correctness cannot override a local target difference.

Resolve a target only when its complete required behavior is demonstrated; a matching function or a locally correct fragment is insufficient when the target's output cannot be produced. If old targets are resolved but another SQL error remains, report the remaining error through applicable unit feedback, without inventing new target IDs. The controller will re-diagnose the latest failed candidate; resolved old targets do not mean the case is complete.

Report only observed deterioration against supplied prior_evidence. Attribute it to candidate units only when supported; otherwise retain attribution uncertainty. Do not invent regression or causality.

# Feedback and output

Return only schema-conforming JSON covering every unit/target exactly once. quality_checks contains exactly quality_requirements. Overall quality is invalid if any required check is invalid, otherwise inconclusive if any is inconclusive, otherwise valid.

Every unsuccessful conclusion needs specific feedback: exact supplied node/relation ID, concrete gap, necessary semantic correction and correct behavior to preserve. Merge repeated instances of one problem; no passing-item narratives or per-attempt repetition. Do not weaken a substantive defect to fix format, expand targets or prescribe replacement graph edits. Couple only semantically inseparable units. The controller determines acceptance from these conclusions, SQL verification and group constraints.
