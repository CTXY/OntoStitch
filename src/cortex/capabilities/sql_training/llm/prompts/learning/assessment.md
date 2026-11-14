# Task

Assess proposed ontology knowledge for one NLQ–Gold SQL case independently. Separate content quality, SQL application, fixed-target resolution and regression. Do not edit the graph, create new targets or authorize final acceptance.

{shared_extraction_contract}

# Inputs

units is the exact inventory. Each supplies candidate, optional accepted predecessor, quality_requirements, application_requirement and recorded context_facts. quality_rubric defines the applicable checks. Read related_knowledge and supplied relation contracts together with units; definitions need not be duplicated. Historical definitions are comparison evidence, not current assertions. A missing contract cannot have valid predicate_precision.

case and physical_context supply business/database evidence. attempts supplies all SQL verification for this candidate. context_facts establishes inclusion, not quality or application; actual presence overrides incomplete selection tracing, and explicit absence overrides selection. Do not invent inclusion paths. change_groups controls joint retention. round_baseline is an older comparison, not the accepted predecessor. fixed_targets and prior_evidence apply only when supplied.

# Evaluation

## 1. Content and consistency

Evaluate exactly each unit's quality_requirements using the shared contract and rubric. Review Metric/Rule content before lengthy edge inventories; valid edges do not prove node quality.

For minimality failure give units, distinct field/value/operation matches and composition. For relation defects identify incompatible endpoint meaning, predicate contract or scope. Compare related Rules' shared decisions and overlapping conditions, including obligations embedded in compound actions; separate Gold support does not prove compatibility.

For redundant assertions identify equivalent facts or a declared rule and independent remaining premises. Preserve limits, consumers and direct operands; assess deletion against the final remaining graph, not mutually deleted witnesses. Report concrete defects, not missing-knowledge coverage or preferences for smaller graphs.

Assess optional relation_metadata under its owner's support/graph_consistency; no extra assessment object or SQL requirement. Use inconclusive only for a specific material gap preventing a mandatory check or interpretation of the actual claim, and identify the gap.

## 2. SQL application

Follow each unit's application_requirement. For sql, verify its candidate-owned behavior in every attempt. For semantic, supported relations/representation edges need no new SQL syntax. For none, check deletion safety.

For Concepts check request-relevant meaning, population and required matching behavior, not selection of every target or identifier. For Metrics/Rules verify the calculation/decision with required operands and supported grain/NULL behavior. Context inclusion alone is insufficient; equivalent SQL is allowed.

Return applied only for complete behavior and recorded inclusion in every attempt. Distinguish partial application, included-but-unimplemented behavior and missing attribution using the schema. not_applicable is allowed for deletions or semantic units without a current SQL obligation. An unrelated SQL error does not negate complete local application; whole-query equality does not prove it. Never combine behaviors from different attempts into one success.

## 3. Targets and regression

Assess every supplied fixed target across all attempts; return no target conclusions when none exist. Whole-query correctness cannot override a local target difference.

Report only observed deterioration against supplied prior_evidence. Attribute it to candidate units only when supported; otherwise retain attribution uncertainty. Do not invent regression or causality.

# Feedback and output

Return only schema-conforming JSON covering every unit/target exactly once. quality_checks contains exactly quality_requirements. Overall quality is invalid if any required check is invalid, otherwise inconclusive if any is inconclusive, otherwise valid.

Every unsuccessful conclusion needs specific feedback: exact supplied node/relation ID, concrete gap, necessary semantic correction and correct behavior to preserve. Merge repeated instances of one problem; no passing-item narratives or per-attempt repetition. Do not weaken a substantive defect to fix format, expand targets or prescribe replacement graph edits. Couple only semantically inseparable units. The controller determines acceptance from these conclusions, SQL verification and group constraints.
