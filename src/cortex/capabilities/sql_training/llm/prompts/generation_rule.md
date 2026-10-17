# Role

Extract Initial Generation Rule candidates.

# Input

- `question`
- optional `evidence`
- `sql`, the Gold SQL used only as implementation evidence
- `database_dialect`; `sql` has already been normalized in this same dialect
- `gold_column_metadata`
- `gold_join_path_column_sets`, sets of ON-column IDs for complete join paths found in Gold SQL;
  each set represents either one direct join edge or one pure two-hop route
- `gold_join_paths_with_intermediate_table`, table and column evidence for pure two-hop routes;
  this is supporting evidence for routing paths, not the Rule reference contract
- `direct_join_alternatives`, any catalog join edge directly connecting the same two tables;
  each edge includes its ON conditions and may represent a different relationship
- `gold_join_fragments`, JOIN clauses copied exactly from Gold SQL
- `database_context` and `physical_reference_catalog`
- current-case Concept and Metric candidates, used only to separate owners

# Generation Rule

Interpret Gold functions, operators, quotes, casts, and date expressions in `database_dialect`.
Do not reinterpret MySQL Gold as SQLite or SQLite Gold as MySQL.

A Generation Rule contains one causal chain: condition, action, and reason.

- `condition` is the smallest NL condition that makes the whole action necessary.
- `action` is one reusable SQL-generation behavior. It may name SQL operations and the physical
  fields needed to explain them clearly.
- `reason` compares the action with another plausible implementation of the same requested
  operation and states the observable result difference.
- `operation_intent` is a direct Rule field that records which Registry intent guided discovery.
  It does not replace `condition` or `action`, define Rule identity, or prove correctness. Do not
  create a metadata object.

A Rule does not define a Concept binding or repeat a Metric formula/plan. It is not ordinary SQL
syntax, a current literal, a one-off patch, or several independent behaviors combined together.

# Initial Operation Registry

Use the Registry as an attention list, not a SQL operator list or template library. A Registry
match only tells you to inspect a possible Rule. Question meaning, Gold grounding, owner boundaries,
and the condition-action causal link must still pass independently.

Reference implementations show common result differences. They are neither fixed templates nor an
exhaustive list. Each implementation is a complete alternative, not a fragment to combine with
another implementation. Do not force an unlisted behavior into the nearest Intent.

{generation_rule_taxonomy}

# Extraction tests

Emit a Rule only when all tests pass:

1. The Question expresses one Registry intent.
2. Gold directly implements the same user-level behavior.
3. Another plausible implementation of the same intent can change value, rows, order, duplicates,
   ties, cardinality, or result encoding.
4. The behavior is not already Concept- or Metric-owned.
5. Replacing current inputs keeps the same condition-action relationship.
6. The Rule has one smallest cause and one complete reusable behavior.

Evaluate each atomic Registry intent independently. If one requested operation has unclear or
mismatched lineage, omit that Rule without suppressing another operation that Question and Gold
both prove.

SQL syntax alone never creates a Rule. Ordinary predicates, member equality, direct JOINs, CTEs, query
transport, plain projection, and SELECT are outside this Initial Registry. DISTINCT is outside
unless it is the Gold-proved action of `result_deduplication`. Words such as “list”, “show”,
“state”, and “all” do not establish a duplicate policy. SUM, AVG, ratio, growth, rounding, and
zero/NULL behavior stay in Metric when they intrinsically implement the named Metric.

A standard aggregate request over a complete Concept, entity set, stored value, or existing Metric
is Rule-owned. In particular, “how many X” or a compositional “number of X” uses
`count_aggregation`; X keeps its own owner. Do not emit this Rule when the complete term instead
names a business measure whose own definition requires the formula or a composite counting unit.

# Intent and Case boundaries

- Use `joinability` when the Question requires choosing the relationship path or key relationship
  between already-complete meanings. The required path may be a direct join edge or a pure route
  through one intermediate table. If an intermediate table also supplies a value for SELECT, WHERE,
  GROUP BY, ORDER BY, or a calculation, do not treat that table as a pure routing table.
- The Rule's `references` must equal one complete set from `gold_join_path_column_sets`: every
  Column used by that path's ON conditions, and no Table IDs or unrelated Columns. This set proves
  which complete path the Rule governs. The Question must separately establish when that business
  relationship is needed.
- Check each supplied complete join path separately before returning `assets`. If the Question asks
  for the relationship recorded by that path, include its Rule even when another Rule covers a
  filter, ranking, or calculation in the same query. If the Question does not support the
  relationship, omit the Rule; the SQL path alone is insufficient.
- State that business relationship in `condition`, without naming SQL, tables, or keys. State the
  selected path and its ON conditions in `action`. In `reason`, explain how a competing path or key
  relationship could change the requested rows or values.
- Remove a current demographic, time, status, or value filter from `rule_name`, `condition`, and
  `action` when removing it would leave the same connection between the tables. The exact Question
  phrase in a trigger may still contain that filter. Name the business relationship requested by
  the Question, rather than the intermediate table.
- If the Question does not establish when this relationship should be used, omit the Rule even
  when Gold contains the route. Do not turn the current SQL shape into a universal instruction.
- Compare any supplied `direct_join_alternatives` by their ON conditions when writing `reason`.
  If one exists, do not claim that no direct join exists. Explain how it could connect different
  rows. If none is supplied, do not infer from that absence that no other join exists.

- Decide Intent from the result requested by the Question, not from a shared SQL function.
- Use `boolean_output_encoding` when the Question asks for a yes/no answer and Gold returns labels
  such as `'YES'` and `'NO'`. The action must describe the final SELECT expression, not a WHERE
  filter.
- Use `result_deduplication` only when the Question requests the output item or tuple as the result
  and Gold proves that duplicate source rows must be collapsed.
- Trace the complete relevant operator chain and describe its final observable result. A later
  limit, aggregation, or deduplication can change the rows produced by an earlier step.
- Gold must rank or select the same quantity, grouping entity, and carrying entity named by the
  Question. If these lineages differ, emit no Rule even when both contain MAX, COUNT, or LIMIT.
- `SELECT MAX(value)` returning one scalar is `extreme_value_selection`.
- A row predicate using a nested `MAX(value)` to return the carrying rows is `top_k_selection`; the
  nested MAX is not a second Rule.
- `RANK()` used only inside a top-K implementation does not create `rank_output`.
- Use `rank_output` only when the Question asks to return rank values.
- Never claim that all ties are preserved when a later operation can remove tied rows.
- If Gold limits an ordered result to one row, the action must return one row. Do not replace that
  behavior with a tie-preserving minimum/maximum predicate.
- The action states one complete result contract: for Top-K, say whether it returns at most the
  requested K ordered rows or retains all boundary ties. The reason must describe the observable
  difference from an alternative contract; it must never attribute the alternative's tie or
  cardinality behavior to the stated action.
- The Intent label does not set Rule boundaries. Separate behaviors that can be triggered and reused
  independently, even when they share one Intent.

# Condition, action, and reason

- Build `condition` only from meaning visible in the Question before SQL generation.
- Keep only the business request visible before the Rule's decision. Do not put the required SQL
  representation, chosen operation, postcondition, or contrast conclusion into `condition`.
- Every fact in `condition` must be supported by the exact Question trigger. Do not infer
  JOINs, subqueries, Tables, Columns, keys, NULLability, duplicate rows, or Schema cardinality.
- Start from the exact Question operation. Remove each condition clause whose deletion leaves the
  action unchanged.
- Use only the Question's business words needed by condition and action prose. Do not replace them
  with abstract prose placeholders.
- Omit current dates, thresholds, K, entity values, and member values. Refer to them naturally, for
  example “the date specified in the Question” or “the requested number of results”.
- State SQL operations and necessary Table/Column bindings directly. Include a local parameterized
  `SQL pattern:` when prose alone does not fix the operation choice or order, especially for window
  functions, aggregation before ranking, date boundaries, or result encoding. Define every
  role-labelled placeholder in the surrounding business prose. Keep the pattern at this decision
  point; do not copy a full query, current literal, alias, or CTE layout. Simple unambiguous actions
  do not need a pattern.
- Start `rule_name` from the Question's operation phrase. Keep its concrete business inputs when
  condition, action, or references keep them; use a general name only when the complete Rule uses
  replaceable roles. Do not replace a retained business term with “group”, “metric”, “entity”, or
  another broader class word in the name.
- Keep several SQL steps in one action only when each is necessary for the same semantic behavior.
  If a step can be triggered and reused independently, put it in a separate Rule.
- Make `action` no broader than the Gold-proved behavior.
- Make `reason` contrast the action with another plausible implementation of the same requested
  operation. Omitting the operation or returning every unfiltered value is not an alternative.
- Check action and reason as one result contract. They must agree about rows, cardinality, order,
  ties, values, and NULL behavior of the stated action. If they do not, emit no Rule.
- Do not write an Intent name or reference implementation name as `condition` or `action`.

# Evidence, triggers, and references

- Copy each trigger as an exact contiguous Question phrase. Keep its source values unchanged.
- Put parameterized applicability in `condition`, not in the source trigger.
- `evidence.gold_sql_grounding` contains the smallest exact Gold SQL substrings that jointly prove
  the action, including every later operator that changes its final rows or cardinality. It is
  evidence only and is not persisted.
- For a join through an intermediate table, cite the relevant `gold_join_fragments` separately.
  Do not combine clauses separated by another JOIN into one quoted substring.
- Do not output SQL-side Column or operation triggers; the compiler sets both to `[]` for Initial
  Extraction.
- Keep only exact IDs from `condition_reference_catalog` for assets that the condition or action
  cannot work without. All listed references must hold together
  for the complete Rule contract.
- Do not reference a replaceable implementation detail, a node used only by the reason, or a node
  that merely co-occurs in the query.
- Write Rule prose and `rule_name` in the Question's language; keep SQL syntax and role placeholders
  in the local `SQL pattern:` unchanged during language correction. Do not include the wrapper words
  `WHEN`, `MUST`, or `BECAUSE` inside their fields.

# Boundary examples

## Returning a scalar maximum

Question: “What is the highest stored score?”

Gold: `SELECT MAX(result.score) FROM result`

Extract `extreme_value_selection`. The condition asks for one scalar highest value; the action
returns the scalar maximum. Do not extract top-K because no carrying row is requested.

## Returning the rows at the maximum

Question: “Which results have the highest stored score?”

Gold: `SELECT result.id FROM result WHERE result.score = (SELECT MAX(result.score) FROM result)`

Extract `top_k_selection`. The action returns every carrying row tied at the maximum. The nested
MAX is an implementation substep, not a second `extreme_value_selection` Rule.

## Omitting one current input

Question: “List schools opened after 2000-01-01.”

Gold: `schools.OpenDate > '2000-01-01'`

Use the business term and describe the supplied date naturally:

- condition: `The Question asks for schools opened after a specified date.`
- action: `Keep schools whose opening date is later than the date specified in the Question.`

Do not keep `2000-01-01`, and do not replace the business words with a placeholder.

## Aggregating before ranking

Question: “Which product has the highest order quantity in each category?”

Explain the business roles, then make the easily confused operation order explicit:

```text
Rule name: Select the product with the highest order quantity in each category
Aggregate order quantity by category and product, then rank products inside each category.
SQL pattern: RANK() OVER (
  PARTITION BY <category_column>
  ORDER BY SUM(<order_quantity_column>) DESC
)
```

Do not omit `SUM(...)`, replace `RANK()` with `ROW_NUMBER()` when ties must be retained, or expand
this fragment into the current query's CTE plan.

# Output

Each output asset has these fields:

- `operation_intent`: exactly one Registry intent.
- `rule_name`: a short operation name that preserves the complete Rule's business inputs and
  generality.
- `triggers.question.phrases`: exact Question phrases; each phrase is an independent OR retrieval
  signal.
- `evidence.gold_sql_grounding`: exact Gold SQL substrings used only for validation.
- `condition`, `action`, and `reason`: the three parts of the Rule.
- `references`: exact dependency IDs that the condition or action cannot work without, or
  `[]`.

Do not output `metadata` or `triggers.sql`. They are not part of the Initial output schema.

Return `assets` matching:

{template_output}

Return `assets=[]` when no candidate passes every test. In `<reasoning>`, show the matched Intent,
exact Question phrase, exact Gold grounding, owner check, alternative behavior, and atomic causal
chain for each emitted Rule.
