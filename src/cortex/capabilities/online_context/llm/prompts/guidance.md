# Task and authority

Organize task guidance from `question` (including supplied evidence), answered
`slots`, `user_answers`, and `rule_candidates`. You do not receive Gold SQL.
Preserve their meaning; do not make another SQL decision.

# Confirmed answers

1. Process every Slot once, in input order.
2. If Candidate IDs are selected, copy those Candidates' statements in order.
   Otherwise copy the answer's `explanation`. This is the confirmed behavior.
3. Write `For <short purpose>, MUST USE: <confirmed behavior>`. The purpose
   identifies the answered decision only; it must not add or change behavior.
4. Keep the answer within its Slot. Selecting an identifier does not also select
   output fields, JOIN keys, DISTINCT, a counting grain, or a population.
5. Resolve phrases such as "the selected identifier" only through another
   confirmed answer. Do not substitute a default or decide an unanswered Slot.

# GenerationRule applicability

Assess every candidate Rule once. Retrieval and a matching operation label do not
establish applicability. Include a Rule only when all of the following hold:

- Its condition is established by the question/evidence or confirmed answers.
- Its action applies as written, without conflicting with a confirmed answer or
  selecting an unresolved alternative.
- It requires no additional task-specific choice to be stated correctly.

Apply the test to EVERY behavioral clause in the original action, not just its
operation label or condition. Check the requested output role, physical source,
operands, population, aggregation grain, NULL/DISTINCT policy, ordering and limits
where relevant. Evidence must come from the current request, confirmed answers
and their established bindings; a Rule's own historical reason cannot establish
what the current request asks for.

In particular:
- An extreme-value Rule that returns another attribute of the selected row does
  not apply just because the current request also asks for an extreme value.
  The returned attribute must match the current requested output. Asking which
  entity has the largest amount does not authorize returning a stored balance.
- A Rule for DISTINCT (identifier, description) pairs does not apply to a request
  for descriptions alone. Deduplication does not authorize extra output columns.
- A Rule over a filtered population cannot settle an unresolved question about
  whether the extremum is global or restricted to that population.
- Confirming one clause (such as tie handling) does not confirm neighboring
  clauses (such as output field, ranking population or aggregation grain).

If any clause conflicts or requires an unresolved choice, exclude the ENTIRE
Rule. Do not salvage a matching clause, specialize the action into a new Rule,
or use one included Rule to justify another conflicting Rule. Check included
Rules together as well as individually; incompatible actions cannot both become
instructions. Explain the concrete mismatch in the exclusion reason.

For each Rule, return only its supplied short `rule_id`, `include` and `reason`.
The Controller restores the real ID and copies the original `action` unchanged
into the downstream instruction for an included Rule. Do not emit `instruction`
or a rewritten action. Keep generic actions generic. Do not fill placeholders,
specialize a formula, substitute task literals, or combine Rules into a new
action. If a Rule cannot be used as written, exclude it with a short reason.
Exclusion is task-local; it does not declare the stored Rule invalid.

# Preserve scope

- Interpret supplied evidence together with the question. A shorthand field name
  or formula does not authorize a new physical binding or calculation.
- Preserve the entity, conditions, population, operands and output role already
  specified by the question and confirmed answers.
- For a ratio within population P, P limits both numerator and denominator.
  Do not turn it into an unrestricted numerator divided by the count of P.
- Do not infer a table from a bare field name when multiple sources are possible.
- Preserve explicit evidence formulas, their numerator/denominator, sign, scale,
  date bounds and output direction. A counting-method answer does not change a
  supplied ratio formula; an age-expression answer does not determine MIN/MAX.
- An unasked or unanswered decision is not permission to complete it. Do not add
  filters, JOINs, outputs, formulas, DISTINCT or limits to make guidance concrete.
- Check that the combined guidance preserves the confirmed answers and does not
  contradict the question's scope. Do not silently repair a conflict by inventing
  an implementation.

# Output

Return one JSON object inside `<result>...</result>` matching the schema below:

- `guidance_by_slot`: one `{choice_point_id, guidance_content}` per Slot, in order.
- `rule_guidance`: one `{rule_id, include, reason}` per supplied short Rule ID (R1, R2, ...). Do not copy actions or emit instruction. The Controller restores real IDs and exact actions.
