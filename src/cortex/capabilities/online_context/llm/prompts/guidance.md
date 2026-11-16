# Role and inputs

Produce SQL guidance from the question/evidence, answered Slots and task-local
GenerationRule candidates after user-choice projection. You do not receive Gold
SQL. Return both outputs in one response; do not call a separate model per Rule.

# Confirmed choices

For each Slot in input order, copy its ID and write:
`For <short concrete purpose>, MUST USE: <exact confirmed behavior>`

Selected Candidate statements, in order, are the confirmed behavior. Ignore the
answer's explanation when Candidate IDs are selected; otherwise use the exact
explanation. Do not repair, expand, omit or normalize this behavior. The purpose
only identifies its scope, without introducing another decision or rationale.

# GenerationRule applicability

Assess every `rule_candidates` item exactly once. Retrieved Rules are historical
knowledge, not confirmed task requirements. Include a Rule only when:
- Its condition is supported by the question/evidence or confirmed choices.
- Its action serves this task, without conflicting with confirmed choices or
  depending on an unselected conflict branch.
- It does not impose extra outputs, filters, joins or aggregations outside that
  condition. A matching operation label alone is insufficient.

A Rule may apply without causing a clarification question. For an applicable Rule,
write its action concisely, retaining its meaning, scope and limits. Otherwise give
a short exclusion reason and an empty instruction. Do not invent a new Rule.
Exclusion is task-local, not a judgment that stored knowledge is invalid.

# Output

- `guidance_by_slot`: one `{choice_point_id, guidance_content}` per Slot in order.
- `rule_guidance`: one `{rule_id, include, reason, instruction}` per candidate Rule. Copy each input `rule_id` exactly; include excluded Rules with `include: false`, a reason and empty instruction. Do not omit them.

Return exactly one JSON object inside `<result>...</result>` matching:
{template_output}
