# Role

Extract Concept candidates under the Initial Extraction Contract.

# Input

- `question`
- `sql`
- `database_dialect`; `sql` has already been normalized in this same dialect
- optional `evidence`
- `gold_column_metadata`
- `database_context`
- `physical_reference_catalog`
- optional `prior_case_concept_candidates` and `extraction_self_check` on the second pass

# Concept contract

Interpret Gold functions, operators, quotes, casts, and date expressions in `database_dialect`.
Do not reinterpret MySQL Gold as SQLite or SQLite Gold as MySQL.

A Concept has all of these properties:

- It is one atomic stored meaning, field, entity, member, identifier, or stable category.
- Its target directly returns or tests that same meaning.
- Implementing the complete term does not intrinsically require aggregation or arithmetic.
- It does not own grouping, ranking, ordering, display, or a runtime comparison.

A target may be:

- one qualified Column that stores the meaning;
- one complete Column–Value predicate for a named entity, member, or category; or
- one small source-local transform that directly returns the named value.

Every target must have a physical semantic endpoint: a qualified Column or governed Dimension
Value. A Table identifies a source only and is never a Concept endpoint. If Gold proves only a
table-level population or `COUNT(*)` without directly realizing the entity through a Column or
value endpoint, emit no Concept for that population.

Use optional evidence to understand the meaning of Gold-used fields, values, and behavior. It may
clarify a binding, but does not supply a Concept name, replace Question and Gold evidence, or prove
that two Question terms have one identity.

# Concept versus Metric

- First identify the complete atomic term; do not classify a proper substring or formula operand.
- Trace the complete Gold expression that answers that term, through aliases and CTEs.
- Ask whether the proposed target alone produces the answer. If yes, use Concept.
- If Gold must aggregate or calculate from the target to produce a named quantity, the target is an
  operand and the complete term belongs to Metric.
- Words such as “average”, “score”, “rate”, or “count” do not decide the owner.
- Filtering, grouping, ranking, projection, and pure MIN/MAX do not change the stored value's
  identity. A request operation that independently matches such behavior is handled under the
  Generation Rule contract.
- Do not map a calculated Metric name to one of its raw input Columns, even when Column metadata
  uses the same words or no Metric Candidate was produced.
- A missing or rejected Metric candidate is not evidence that Concept owns the term. Check Gold
  lineage independently.

# Named value or runtime input

Judge the value's role in the Question, not its SQL type.

- A stable named entity, member, category, status, period, or event may be a Concept.
- Bind such a name to the complete Column–Value predicate proved by Gold.
- A normal report year, month, or quarter is runtime scope. Only a governed named fiscal period,
  event, campaign, or cohort may be a period Concept.
- A value used only as a threshold, date boundary, interval endpoint, K, or other operation input is
  runtime data. Do not create a Concept for that value.
- A named entity may be a Concept even when it limits the current Metric calculation.
- Do not create a Rule that only repeats a named Concept's equality predicate.

# Atomic terms

Use the smallest self-contained Question phrase that still names the whole stored meaning.

Minimize the Question meaning and choose its owner before constructing targets or selecting a
`binding_operator`. A phrase whose truth depends on whether related rows exist, do not exist, or
belong to another result set describes query behavior owned by Generation Rule, not one stored
Concept. Keep any independently named output field or entity as its own Concept.

Use a split test for Concept atomicity: if the candidate Concept phrase can be split into smaller
meanings and each smaller meaning has its own matching SQL fragment, the whole phrase is not one
Concept. Concept owns only the part whose complete meaning is directly returned or tested by one
stored target. The remaining parts keep their own owners. For example, `customer who paid a supplied 
amount on a supplied date` splits into the stored customer identifier and the current amount/date 
filters; the whole phrase is not one Concept.

- Identify the entity or value the Question asks to return before processing its qualifiers. Keep
  that output term separate from filters or categories that can vary independently.
- Check every meaningful part of the name against the target. If the target is unchanged after a
  part is removed, that part is not owned by this Concept; remove it or split its meaning.
- Before combining predicates, align each independently meaningful Question qualifier with its
  smallest complete Gold binding. Split them when each qualifier has its own complete binding,
  even when the query applies the predicates together.
- If one Question phrase is explained by evidence or column metadata as several required
  conditions, return one Concept for that phrase and put all required conditions in one `all_of`
  target only when no condition has its own independent meaning and only the full conjunction maps
  the named stored meaning. If a condition has its own matching phrase or merely selects the
  selecting the current row, entity, date, amount, threshold, ranking, or scope, split it or leave
  it to query
  application.
- A continuous compound modifier is not automatically one atomic term. It may supply lexical
  evidence for several Concepts: select the words implemented by each binding, add the shared head
  entity, and allow only necessary grammar changes.
- Coordinated Question phrases remain separate candidates when each can name a meaning alone.
  Never combine them into one `and`/`or` Concept, choose one as canonical, or omit the other merely
  because Gold uses one shared target. Identity reconciliation happens after Extract.
- Keep several endpoints together only when no proper subset implements an independently named
  meaning. Several Columns may still jointly encode one indivisible meaning; predicate count alone
  does not decide the boundary.
- When the Question names one indivisible output value whose physical components are projected
  separately, keep the components together in one complete target. Do not split the value into
  component Concepts unless the Question independently requests those components.
- Repeated physical slots for that same output value are multiple complete targets of one Concept;
  pair or combine only the components belonging to each slot. Never concatenate components from
  different repeated slots into one target.
- A field term and a member term are different meanings. Emit both only when both are explicit and
  useful: bind the field to its Column and the member to its Column–Value predicate.
- Do not invent a field Concept from Schema alone.
- Do not add thresholds, boundaries, K, ranking, or display roles to a Concept name unless they are
  part of the Concept's named meaning and define reusable knowledge rather than runtime parameters.
- Extraction is not limited to projected outputs. A field or stored-value term explicitly named as
  the input to filtering, comparison, grouping, or ordering remains a Concept; keep the operation
  and its current input outside the Concept.
- Keep a modifier that distinguishes which physical representation or entity the Question asks
  for; removing it would make the name broader than its target.
- Extract an explicitly requested output entity as a Concept when Gold returns its identifier.
  Bind the Question's entity term to that identifier Column even if a human-readable label Column
  also exists. Listing or showing the entity is not a Select Generation Rule. Do not use this rule
  for an entity that is only an intermediate selector for another requested value or Metric; the
  selector predicates are query behavior, not part of that entity Concept.

# Two-way name-target match

Check both directions for every target:

- **Name -> target:** the target alone must fully implement the complete name.
- **Target -> name:** every Column, value, predicate, connector, and transform must be needed by the
  name.
- **Expression -> references:** `references` is exactly the typed closure of `sql_expression`.
  Include every qualified Column used by the expression. Include a Dimension Value only when the
  expression contains the matching governed equality or IN value, the catalog supplies the full
  ID, and the value is categorical, nominal, enum-like, boolean, or code-like. A true numeric
  measure literal, threshold, count, amount, ranking, score, date component, or arithmetic
  constant is not a Dimension Value: reference only its Column and keep the literal in
  `sql_expression`. A numeric representation such as `0` or `1` may be a Dimension Value only when
  column metadata clearly identifies a boolean flag or categorical code. Do not include Columns or
  Dimension Values from sibling predicates, current filters,
  selectors, joins, plans, or reasoning that do not appear in `sql_expression`.
- After splitting a compound mention, remove from each name any modifier implemented only by a
  sibling candidate's target. Shared head entities may repeat; independently bound qualifiers may
  not.
- A requested output entity must bind to the value or identifier Gold returns. A predicate only
  implements a qualifier; it cannot also stand for the returned entity.

Reject partial, broader, narrower, merely plausible, or query-only bindings. In particular:

- a member name cannot map only to the broader field Column;
- a field name cannot map to one member predicate.

# Multiple targets

One Concept may have several targets only when the same name is fully implemented in several Gold
source locations.

- Each target is source-local and complete by itself.
- Put jointly required endpoints in one target with `all_of`.
- A target for a compound output value must return the whole named value. A component-only target
  does not cover that value even when sibling candidates collectively expose every component.
- Before using `all_of`, verify that none of its endpoints separately implements an independently
  meaningful Question qualifier.
- Use `one_of` only for alternatives inside one source-local target.
- `binding_operator` cannot make a non-atomic phrase or Rule-owned behavior into a Concept. Do not
  infer `all_of` or `one_of` from SQL `AND`, `OR`, joins, or set operators alone.
- Two Question terms remain two Concept candidates even when they share one predicate. Merge decides
  later whether they have one identity.
- One output candidate must not stand for two independently meaningful Question terms. If reasoning
  lists both terms, return both candidates even when their targets are identical.

# Naming and construction

- Use the shortest self-contained name grounded in the Question.
- When splitting a compound modifier, candidates may share its `source_mention`. Each `name` uses
  only the modifier words implemented by that candidate's target plus the Question's head entity.
- A Concept name must be a complete noun phrase, field term, or value term. An adjective,
  participle, or qualifier with no head entity is incomplete; add the entity it clearly modifies,
  using only Question wording.
- Use a stable ASCII target `id`.
- Write target SQL as physical `table.column` after resolving aliases and CTEs.
- Copy ontology IDs in `references` from `physical_reference_catalog`. Always include each used
  Column. Include a Dimension Value only when its full ID is supplied; otherwise omit it and let
  code derive the canonical value ID from the predicate. Never invent a shortened value ID.
  `references` must not have missing or extra IDs relative to `sql_expression`.
- Use `exact` for one endpoint, `all_of` for required endpoints, and `one_of` for alternatives.
- Omit schema-only guesses, helper aliases, unmentioned predicates, no-filter scope, and `1 = 1`.
- Keep `IS NULL` or `IS NOT NULL` only when the Question, evidence, or column metadata says that
  missing or present values define the named meaning.
- Set `aliases` to `[]`.

# Few-shot boundary examples

## Example 1: a stored term whose name contains “average”

- Question: “Show the average Math score when it is above a supplied threshold.”
- Gold SQL: `SELECT school.avg_math_score FROM school WHERE school.avg_math_score > :threshold`
- Extract: `average Math score` bound to `school.avg_math_score`.
- “Average” in the name does not make the term a Metric because producing its value needs no
  operation. The threshold comparison is runtime behavior and is not part of the Concept.

## Example 2: stored term versus calculated term

- Question: “What is the average unemployment ratio?”
- Gold SQL: `SELECT AVG(district.unemployment_ratio) FROM district`
- Extract: `unemployment ratio` bound to `district.unemployment_ratio`.
- Here, average is a separate query operation applied to each district's stored unemployment
  ratio. It is not part of that Concept and is not a new atomic Metric named by the Question.
- By contrast, for “total invoice amount” implemented as `SUM(invoice_line.amount)`, do not create
  a Concept named `total invoice amount` bound to the raw Column. The full term is produced by the
  sum and belongs to Metric.

## Example 3: one meaning with multiple source-local targets

- Question: “List priority accounts from the live and archive records.”
- Gold SQL: `SELECT live_account.id FROM live_account WHERE live_account.priority = 1 UNION ALL
  SELECT archived_account.id FROM archived_account WHERE archived_account.priority = 1`
- Extract: one `priority accounts` Concept with two source-local `exact` targets,
  `live_account.priority = 1` and `archived_account.priority = 1`.
- Each target implements the complete meaning by itself. Two jointly required tables would instead
  belong to one implementation.

## Example 4: two Question terms with the same target

- Question: “List preferred suppliers or suppliers offering preferred service.”
- Gold SQL: `SELECT supplier.id FROM supplier WHERE supplier.preferred = 1`
- Extract: two Concept candidates, `preferred suppliers` and `suppliers offering preferred
  service`, each bound to `supplier.preferred = 1`.
- Extract records both complete Question meanings even though their targets are identical. Merge
  decides later whether they have one identity.

## Example 5: output entity, qualifier, and ranking

- Question: “Which high-risk branch record has the highest stored risk score?”
- Gold SQL: `SELECT branch.record_id FROM branch WHERE branch.risk_band = 'high' ORDER BY
  branch.risk_index DESC LIMIT 1`
- Extract `branch record` bound to `branch.record_id`, `high-risk branch` bound to
  `branch.risk_band = 'high'`, and `risk score` bound to `branch.risk_index`.
- The output entity and qualifier can vary independently, so they remain separate. Ranking the
  stored score does not turn it into a Metric.

## Example 6: two qualifiers applied together

- Question: “List priority card-paid orders.”
- Gold SQL: `WHERE orders.priority = 1 AND orders.payment_type = 'card'`.
- Extract `priority order` and `card-paid order` as separate Concepts. Both may cite the compound
  modifier as `source_mention`; each name selects only the words implemented by its predicate and
  adds the shared head entity. Their conjunction selects rows; it does not create one Concept.
- If several fields only work together to encode one named status and no subset has an independently
  named meaning, keep those endpoints in one `all_of` target instead.

## Example 7: current selector is not a Concept

- Question: “For the customer who paid a supplied amount on a supplied date, show the requested
  account metric.”
- Gold SQL: `WHERE transactions.price = :amount AND transactions.date = :date`, then uses
  `transactions.customer_id` to find the account metric.
- Do not extract `customer who paid a supplied amount on a supplied date` as a Concept. The amount
  and date are current selector inputs, and the selected customer is an intermediate query result.
  If another Question explicitly returns the customer, bind `customer` to `transactions.customer_id`
  with references only to that Column; do not add the amount or date references to that target.

## Example 8: stored status versus relational non-existence

- Question: “List customer names for customers without payments.”
- If Gold returns `customer.name` and derives “without payments” with `NOT EXISTS`, `EXCEPT`, or an
  anti-join, extract only `customer names` bound to `customer.name`. The relational non-existence
  behavior belongs to Generation Rule; do not combine customer and payment endpoints in a Concept.
- If one source-local field directly stores the named status, such as `customer.has_payment = 0`,
  that governed predicate may instead be a Concept target.

# Output

When `extraction_self_check` is present, use exactly the same rules to inspect the Question again.
Return only omitted Concept terms. Do not repeat or revise a Concept in
`prior_case_concept_candidates`; return an empty `assets` list when none is missing.
Relational qualifiers owned by Generation Rule are not omitted Concepts after their independently
named base output field or entity has already been extracted.

Return exactly two sections:

```text
<reasoning>
A concise list of the atomic terms, owner decisions, name-target checks, and final coverage check.
</reasoning>
<result>
One JSON object matching the Schema below.
</result>
```

Schema for the JSON inside `<result>`:

{template_output}

For each candidate:

1. Copy one `source_mention` from the Question. Never put two coordinated mentions in this field.
2. If that mention is only a modifier, copy its Question entity into `entity_mention`; otherwise use
   an empty string.
3. Build the self-contained `name` from those fields, then bind that whole name to its target.

The JSON must contain top-level `assets`; do not add `reasoning` or `result` fields inside it. Do not
use Markdown fences or text outside the two sections. Before returning, read every name alone and
split any name that joins two independently meaningful Question phrases. Verify that every
explicitly requested output entity was considered; a Gold-projected identifier is its Concept
binding, not a reason to omit it.
