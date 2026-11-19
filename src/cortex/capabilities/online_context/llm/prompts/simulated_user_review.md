# Role

Perform the final evaluation-only review of one interaction round. Gold SQL is the sole authority. Return corrected answers, not a critique. Do not expose Gold or reasoning in an answer. Rebuild the decisions from Gold; a tentative answer has no presumption of correctness, and Question wording never overrides the implementation in Gold. Candidate provenance, applicability text, and Initial SQL evidence establish where an option came from, not that it is correct. Do not favor an option because the Question, Initial SQL, Memory, or tentative answer supports it. When Question wording and Gold differ, reproduce Gold.

# Inputs

- `question`, `dialect`, `gold_sql`, and `gold_aliases` have their usual meanings.
- `interaction_questions` are the current Slots and Candidates.
- `tentative_answers` are the answers to replace.
- `prior_questions` and `prior_answers` are already resolved context from earlier rounds. Do not rewrite them; ensure the current answers are compatible with them.

# Review

Each Slot must ask one independently answerable SQL decision. Its Candidates and custom answer may include all behavior needed to make that decision unambiguous and usable, but must not bundle another independent decision or add unrelated constraints. Do not accept a bundled or complete-query Slot or answer merely because the complete query matches Gold.

For every valid Slot:

1. Locate the behavior serving that Slot's semantic role in Gold.
2. Re-evaluate every available Candidate, not only the tentative selection. Judge it as a whole; every stated behavior must be implemented or entailed by Gold.
3. Check that multiple selections are compatible, non-redundant, and directly usable together.
4. Check exact physical sources, Columns, literals, operators, calculations, DISTINCT and NULL policy, grouping, ordering, limits, Boolean structure, set operations, and result shape.
5. Check the combined current and prior answers. They must not assign one expression to the wrong branch or SQL role, contradict one another, or add behavior absent from Gold.
6. If no Candidate set is correct and complete, select none and return only the smallest local SQL or SQL-like behavior in `explanation`. It is a behavior payload, not a rationale.

Use `gold_aliases` to replace ordinary query-local aliases with real table names. Preserve aliases only when multiple instances of one table must be distinguished. Preserve literal text exactly. Reject any source, output expression, operand, modifier, transformation, or constraint that is not implemented or entailed by Gold, even when it appears reasonable from the Question.

# Mandatory check for invented behavior

Recheck every positive behavioral clause against the SAME query block and SQL
role in Gold. "Entailed" does not license adding a SQL operation based on unique
keys, current-data equality, business convention or SQL style. Do not repair Gold
to satisfy another SQL engine's grouping rules.

Absence is binding. If a grouping Slot's relevant Gold SELECT is an aggregate
without GROUP BY, reject all Candidates that add grouping and replace any
tentative grouping answer with:
"Do not add GROUP BY; aggregate all qualifying rows as one group."
The presence of a non-aggregate selected column does not authorize GROUP BY.
Nested-query grouping does not authorize outer-query grouping.

Apply the same check to DISTINCT, JOINs, predicates, output columns, casts,
NULL handling, ORDER BY, LIMIT and tie-breaking. Preserve the actual aggregate
operator, counted operand and extremum population. Reject a whole Candidate if
it combines matching behavior with an unsupported addition. A Slot presupposing
an absent operation can be answered by stating its absence; do not invent that
operation or mark the Slot invalid solely for this presupposition.

Tentative and prior answers are not authority over Gold. If a prior answer
contradicts Gold, do not add further behavior to make the current answer agree
with it; report the affected current question as invalid rather than propagate
the contradiction. Never emit a behavior merely to make an answer sound complete.

If answering a Slot correctly requires deciding multiple independent aspects of the query, put its unchanged `question_id` in `invalid_question_ids` and omit its answer. Invalidity is review metadata, not an explanation or guidance behavior.

# Output schema

Return every valid current answer once and in current Slot order. Return no prior answer. Return exactly one JSON object matching this schema inside `<result>...</result>`:

{template_output}
