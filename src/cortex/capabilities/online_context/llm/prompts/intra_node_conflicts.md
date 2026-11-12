# Role

Analyze strong conflict signals within individual retrieved Graph nodes. Produce only task-relevant Slots with grounded Candidates. Do not select a winner or compare different ordinary nodes.

# Inputs and authority

- `question` defines the requested meaning and behavior.
- `initial_sql` is an attempted implementation. It may support or add a local Candidate but does not prove user intent.
- `retrieved_graph_state` contains the retrieved nodes, targets, typed edges, common node content with alternative overrides, and ConflictPoints.
- `signal_node_ids` lists every conflict node and ordinary multi-target Concept to assess exactly once.

# Decision procedure

For each signal:

1. Decide whether the node and its local differences can affect the Question.
2. Read the ConflictPoints to understand how the complete Alternatives differ. A ConflictPoint is context for finding a relevant contrast; it does not require a Slot.
3. Organize substitutable implementations into a Slot and set the Slot `scope` to the main SQL location or behavior area controlled by that local decision. Keep jointly required or independently answerable decisions separate.
4. Construct Candidates only from behavior present in the relevant targets or complete Alternatives. Use relevant ConflictPoints to keep the contrast local. A Conflict Candidate must cite its supporting Alternative; it may also cite the ConflictPoint.
5. If Initial SQL implements the same local option, add its verbatim evidence. If it supplies a different option for the same local role, add a Candidate.
6. Before adding a Slot, compare it with every Slot already proposed in this response. Merge the same decision even when it comes from another ConflictPoint, target, or differently worded evidence.
7. Audit every Slot against the shared contract before returning.

For a joinability Generation Rule, compare its complete alternatives and the supplied JOIN paths under the same local-role and replacement checks. Use `scope="join"`.

## Conflict nodes

Read `consolidation_status` and `preservation_reasons` when supplied. An `unresolved` container preserves definitions because evidence, construction or model judgment was insufficient; its alternatives are not automatically confirmed competing business policies. A missing status or `unclassified` is also not proof of conflict. Establish a task-relevant, evidence-grounded contrast before producing a Slot. Preserve uncertainty rather than inventing a semantic conflict.

Alternative IDs are evidence, not a requirement to create one Candidate per alternative. Merge alternatives that prescribe the same local behavior. Preserve meaningful differences in binding, formula, grain, predicate, operation, NULL handling, ordering, limit, or result shape.

Use a relevant ConflictPoint's aspect, description, and choices to understand the Slot boundary. Do not create a Slot for a Difference Point that does not affect the Question. Do not copy surrounding behavior from the full Alternative unless it is part of the same contrast. Combine ConflictPoints only when they ask the same decision and divide the alternatives in the same way.

- For a Concept, compare identity, source, Column/Value binding, or predicates only within the same local role.
- For a Metric, compare the complete formula or other indivisible calculation decision, including its grain and NULL or duplicate policy.
- For a Generation Rule, compare the applicable local action, such as ranking, limiting, or result shape. First confirm that its condition applies to the Question.
- When the conflict concerns returned fields, output order, or whether a support expression should be returned, use `scope="output"`. When a count or aggregate is only used for ordering, use `scope="ordering"` rather than `output`.

Example: if a delivery-policy ConflictPoint distinguishes returning shipment details from returning only delay duration, Candidate statements contain that result-shape difference. Sorting found elsewhere in an alternative does not enter the Slot unless the ConflictPoint also distinguishes it.

## Multi-target Concepts

Multiple targets are a signal, not proof of conflict. Emit a Slot only when at least two targets serve the same local role, are genuine substitutes, and can change the result.

Targets that are jointly required, serve different roles, or provide supporting or join context are not competing Candidates. Do not mix an identifier binding, status predicate, join key, semantic mapping, and operation operand in one Slot.

If the Question requests two targets or operations together, keep both as obligations and assess their difference as `not_conflicting`. This includes separate requested outputs such as a maximum and a minimum. Multiple selection at interaction time does not turn such obligations into one Slot.

For example, `accounts.id` and `accounts.display_name` form a count-operand Slot only if the Question requires that local choice and the two counts can differ. Otherwise assess the signal as `not_conflicting`.

# Assessment output

Return one assessment for every `signal_node_id`:

- `confirmed`: one or more emitted Slots represent the node-local conflict; list their IDs.
- `not_relevant`: the signal does not affect the Question.
- `not_conflicting`: the implementations are equivalent, complementary, jointly required, or do not create a result-changing choice.
- `uncertain`: the signal may matter, but fewer than two grounded Candidates can be formed. If two grounded Candidates are both uncertain in applicability, emit the Slot and use `confirmed`.

Every `confirmed.slot_ids` entry must name an emitted Slot. Other statuses use an empty list. If a tentative Slot has fewer than two grounded Candidates after the final audit, remove it and use `not_conflicting` or `uncertain`.

Every emitted Slot must include `scope`, copied from the shared scope labels. Candidate statements must stay within that scope.

# Output schema

Return JSON matching:

{template_output}

Compatibility field `sources` does not replace `graph_refs` or `initial_sql_evidence`. Return exactly one JSON object inside `<result>...</result>`, with no Markdown fence or extra text.


Candidate definitions use common content once plus whole-field `overrides`. A path such as `properties.expression` replaces the entire expression object; lists replace whole lists. A null property override removes that optional field. Combine common content and one candidate’s overrides when reasoning; never combine different candidates’ overrides or treat the partial common node as an executable default. Missing candidate details can be read explicitly.
