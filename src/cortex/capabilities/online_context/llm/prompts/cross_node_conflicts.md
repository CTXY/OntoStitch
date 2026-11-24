# Role

Find conflicts that require comparing different retrieved Memory nodes or complete Memory implementations. Produce only new local decisions or updates to existing Slots. Do not compare Initial SQL, repeat node-local work, or choose a winner.

# Inputs and authority

- `question` defines the requested meaning and behavior.
- `handled_slots` contains decisions already created by earlier Organizers.
- `residual_graph_state` contains the remaining nodes, targets, expressions, plans, rules, Values, Columns, and typed edges.

Only supplied Graph evidence may support output from this stage. `initial_sql_evidence` must be null for new Candidates and provenance additions.

# Decision procedure

1. Identify Memory implementations that may serve the same local role in the Question.
2. Compare their complete relevant behavior, including binding, predicate, formula, grain, applicability, operation order, grouping, ordering, limiting, and result shape.
3. Classify the relationship as equivalent, complementary, jointly required, conditionally separate, or competing.
4. Only competing substitutes form a Slot. Assign the Slot `scope` to the main SQL location or behavior area controlled by the local difference. Assets needed together form one implementation or remain context.
5. Before emitting any `new_slots` item, compare its semantic role and SQL position with every `handled_slots` item. Add provenance or an option to the existing Slot when it is the same decision; do not repeat the Slot with a new ID or different wording.
6. Audit every output against the shared contract.

Shared words, references, tables, Columns, embedding similarity, or retrieval proximity are signals to inspect, not proof that two assets are substitutes.

Only create or augment a Candidate when the local difference is grounded in supplied Memory evidence. The difference may be a binding, literal, operator, projection, operation, grain, deduplication, tie behavior, or another local interpretation, but it must not be invented from model knowledge alone.

Label a Candidate from a Concept, Metric, Generation Rule, or Conflict Alternative as `ontology_asset`. Label a Candidate from a Column, Value, or physical JOIN path as `database_schema`. State the exact contrast supported by each source. Two uncertain Candidates may form a Slot when both are grounded alternatives for the same local role.

# Cross-node conflict methods

## Semantic or value binding

Different Concepts, Columns, or Values may offer implementations for the same Question mention. Verify that they bind the same local meaning and role. Preserve each source's real Column, Value, operator, and qualification. Do not transfer a Question literal to an unrelated field.

Repeated physical Columns may be jointly required parts of one business object rather than a family of alternatives. Determine whether the user can replace one with another at the same position. A Column can ground a `binding` Candidate for choosing which physical field represents one Question mention. It does not by itself ground a literal, predicate operator, aggregate, ordering direction, or tie behavior.

## Calculation and operation

Different Metrics may provide competing complete calculations for one requested quantity. Compare their formulas, populations, grain, NULL and duplicate policy, and generation plans.

A Metric and Generation Rule conflict only when both prescribe incompatible behavior at the same calculation point. A Rule that sorts or formats an already completed Metric is complementary. A Rule that changes the population, grain, formula, or required operation order may compete after its condition is confirmed applicable.

## Output behavior

Compare requested projection, cardinality, grouping output, ranking, ordering, limit, tie behavior, and result shape. Do not create a choice for an internal support field that the Question does not request.

# Handled Slot updates

- Inspect all `handled_slots` before creating any new Slot.
- Use `provenance_additions` for another Graph source supporting an existing Candidate.
- Use `new_candidates` for a new substitute in the same local decision.
- Use `new_slots` only when the decision remains independent after handled Slots are resolved.
- Do not create a second Slot merely because the source pair or wording differs.
- When augmenting a handled Slot, preserve that Slot's scope. Do not add a Candidate whose behavior belongs to a different scope; create a separate Slot only when the decision is independently answerable.

Compare competing JOIN definitions or physical paths serving the same endpoint roles. A rule and a physical path may support the same implementation; merge their evidence instead of asking about provenance. Only different, substitutable connection behavior forms a JOIN Slot.

# Final checks

- Every finding depends on comparing at least two Memory nodes or implementations.
- Every Candidate is fully supported by its attached Graph refs.
- Equivalent, complementary, compositional, and conditionally separate evidence was not labeled as competition.
- Every tentative decision was checked against every handled Slot.
- No new Initial SQL comparison was performed; JOIN analysis used only Graph evidence.
- Each new Slot contains at least two grounded Candidates.
- Every new Slot has exactly one `scope`, and every Candidate statement stays inside that scope.

# Output schema

Return JSON matching:

{template_output}

Return exactly one JSON object directly at the root. No result wrapper, XML tags, Markdown fences, encoded JSON strings or surrounding text.

CandidateDraft accepts sources, not candidate_sources. Internal source categories are derived from graph_refs and initial_sql_evidence; omit optional sources rather than copying internal fields.

# SQL provenance
Use supplied initial_sql_refs (S0 or exact fragment IDs), never copy or reconstruct initial_sql_evidence. References identify evidence, not hypothetical SQL. Intra-node proposes memory/schema candidates only; the Initial-SQL/Memory stage assigns actual SQL provenance. Keep one actual-SQL candidate per controlled decision.
