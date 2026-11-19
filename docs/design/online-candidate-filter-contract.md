# Online candidate sources and Filter removals

## Candidate sources

A question may have zero or one candidate citing initial SQL, never two. The SQL-backed candidate describes the actual draft behavior; hypothetical changes do not acquire initial-SQL provenance. Other candidates need supplied ontology/schema support. One candidate may receive both SQL and graph support. When augmenting an existing option, attach support to its existing ID rather than duplicate it.

Validate this constraint both on new question drafts and after extensions. Return the question/candidate IDs and corrective instruction to the Organizer's existing context-preserving repair. Do not choose a candidate, relabel a hypothetical implementation as schema evidence, or extend ranking categories to accommodate invalid provenance. The existing H/M/L ranking remains unchanged.

## Graph references

Graph reference fields contain only exact supplied graph handles. Q1 and E-items are task intent evidence, not graph handles; explain their significance in why/reason. State this in the field schema, shared prompt and validation feedback. A candidate still needs valid support after correction; deleting invalid references alone does not make it admissible.

## Filter basis

The simplified removal interface adds only a required `basis` enum: `explicit_intent`, `database_equivalence`, `duplicate`. Existing reason explains the justification; existing duplicate_of names a prior equivalent question. Duplicate basis requires duplicate_of, and other bases cannot use it.

For database equivalence, reason cites existing probe IDs and explains why successful, complete observations establish equivalence over the whole database. Code validates cited probes exist, succeeded, belong to this question and are not truncated. Failed/missing evidence cannot remove a question. These structural checks do not prove the semantic implication; the Filter must explain NULL behavior, multiplicity, join coverage and other relevant conditions. Common sense invariance or equality only under task filters is insufficient.

Filter only removes questions. It cannot choose a representative implementation, emit MUST USE, or perform not_relevant filtering. Questions lacking a removal proof pass through to Simulator.

The older decision interface in the worktree retains its historical serialized outcomes for compatibility; its basis must agree with question_resolved/database_equivalent. The explicitly frozen repair runtime uses the simplified removal interface, and records basis in the audit decision. Runtime paths and hashes distinguish these interfaces rather than treating folder labels as version evidence.

## Recovery evaluation

Select cases by original online workflow error status, not SQL correctness. Keep accepted graphs and all completed predictions immutable. Run the error cohort with a frozen repaired runtime and fresh checkpoints. Gold SQL is provided only to Simulator after questions and to execution evaluation. Aggregate completion with original predictions over the entire original test denominator, explicitly labeling this as mixed-version recovery, not a uniform new-version experiment. A uniform EA comparison requires a separate complete fixed-version test run.

## Structured response recovery

The compact runtime field contract must retain authored field descriptions for references, SQL support and slot IDs. A missing-support candidate fails with its model path and actionable feedback: provide actual supplied support, or omit the unsupported new question/extension while preserving earlier questions. Empty refs are not a repair.

An otherwise valid single JSON code block with explanatory prose is parsed locally and still undergoes all schema and semantic validators. Multiple code blocks remain invalid rather than guessing a result. This avoids a model repair call for a mere wrapper difference.
