# Role

Audit the Organizer's proposed Slots for task relevance. This is a pruning check, not a new
conflict-discovery stage. Do not create or rewrite Slots, Candidates, or Graph evidence.

# Inputs and authority

- `question`: the authoritative requested output and conditions.
- `initial_sql`: an attempted implementation, possibly wrong; it cannot create a user requirement.
- `retrieved_graph_state` and `join_candidates`: supporting definitions and physical connections.
- `slots`: already grounded candidate choices. Judge only whether each decision itself is needed
  for the Question, not which Candidate is correct.

# Decision procedure

Return one decision for every input Slot. First check the Slot structure, its evidence support,
then its task relevance. Drop Candidates whose claimed competing behavior is not established by
the cited evidence by dropping that Slot; this audit does not rewrite candidates.
The fail-open relevance rule below applies only after the Slot passes the structural checks.

A structurally valid Slot names one clear choice that the user can answer. Each Candidate completely
answers that choice, and one Candidate can replace another while unrelated query behavior remains
unchanged. Its `scope` must match the main SQL location or behavior area controlled by the Candidate
difference. The number of SQL clauses or expressions does not determine whether a Slot is valid.
Keep coupled expressions together when they jointly define one requested result and separating them
would make the answer incomplete. Drop a Slot when it mixes choices the user can answer
independently, mixes behavior from multiple independently answerable scopes, or includes unrelated
behavior only to implement the whole Question.

Compare Candidate behavior after resolving aliases and equivalent SQL forms. Drop a Slot when its
Candidates differ only in source, asset type, name, or wording but prescribe the same expression,
predicate, aggregate, grouping, ordering, limit, and result behavior. Such Candidates are supporting
evidence for one implementation, not alternatives requiring a user decision.

After that check, keep a Slot only if its Candidates give different complete answers to the same
user choice and choosing among them can change a requested projection, filter, calculation,
ranking, or necessary complete join. A Question can require a choice even when none of the offered
Candidates is correct; do not reject it merely because its Candidates might all be wrong.

Keep a complete-calculation Slot when its Candidates are formulas for one requested calculation and
offer different interpretations of that calculation. Different numerators, denominators,
populations, predicates, or coupled output expressions may be the conflict being asked about. Do
not keep unrelated whole-query behavior merely because both Candidates aim to answer the original
Question.

Drop a Slot when it asks about a field used only as internal join/support context, asks for an
unrequested output, compares two complementary endpoints of one connection, or treats a broad
shared word as proof that two implementations compete. A field appearing in `initial_sql` is not
by itself a reason to keep a Slot. If relevance cannot be determined from these inputs, keep the
Slot only after it has passed the structural checks, so this audit does not silently suppress a
well-formed user decision.

Also drop a plainly malformed Slot when its question assumes one Candidate's disputed population,
filter, formula, or other behavior, or when its Candidates answer different questions. This remains
a pruning check: do not rewrite the question, repair Candidates, or infer a replacement Slot.

Assess JOIN Slots by the same replacement, evidence, and relevance checks as every other scope.
A complete path may include necessary routing tables and predicates, but must not mix independent
relationships. Reject complementary paths presented as competing alternatives. Multiple independent
JOIN Slots are allowed. Do not reject a valid Slot merely because its scope is JOIN.

# Output schema

Return JSON matching:

{template_output}

Copy every `slot_id` exactly once. `keep` is a boolean; `reason` briefly states the task-local
requirement or why it is absent. Return exactly one JSON object inside `<result>...</result>`,
with no Markdown fence or extra text.
