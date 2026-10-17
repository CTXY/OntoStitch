# Initial Extraction Contract

Extract reusable knowledge from one Question and its Gold SQL. Keep this case's evidence. Do not
merge identities, infer cross-case rules, or decide acceptance.

## Evidence roles

- The Question supplies business terms, requested operations, and naming evidence.
- Optional `evidence` helps explain the meaning of Gold SQL, including its fields, values, and
  behavior. It is supporting context, not an independent source of terms or implementation. It
  cannot merge, rename, or remove independently expressed Question terms.
- Gold SQL is authoritative for the implementation, formula, operation, grain, and lineage.
- `gold_column_metadata` describes the Columns Gold actually uses. Database context supplies the
  remaining physical identifiers and governed values.
- Existing assets supply known identities; they do not prove what this Question asks for.

Interpret the Question and Gold together; use optional evidence only to understand Gold more
accurately. Do not replace Gold behavior with a more natural alternative. Gold, evidence, or Schema
still cannot create a new business term that the Question does not name.

Evidence and column metadata may explain how one Question phrase is implemented. If they say that
one phrase means several required field conditions together, keep that phrase as one asset name and
bind all of those conditions together. This is different from merging separate Question terms.

## Decomposition and ownership

First split the Question into the smallest terms that can each be matched to their own complete
implementation in Gold. Split a phrase only when the smaller terms and their respective
implementations can each be identified independently.

Use a split test for atomicity: if a proposed asset name can be split into smaller meanings and
each smaller meaning has its own matching Gold SQL fragment, the whole phrase is not one atomic
asset. Keep the part whose complete meaning is directly implemented by its own target or formula;
assign the other parts to their own owners.

For every resulting term, require a two-way one-to-one match: the implementation must completely
express that term, and every part of the implementation must be explained by that term.

Every meaningful part of an asset name must be implemented by its target or formula. If removing a
part leaves the implementation unchanged, remove it or split the independent meaning into another
asset.

Choose ownership before checking whether the current Candidate lists contain that owner. Perform
the same owner test as if all Candidate lists were empty; the result must not change when a
Candidate is absent, incomplete, rejected, or unavailable. Candidate presence is used only to
decide whether the meaning is already covered, never to transfer it to another owner.

- **Concept**: a term whose one-to-one implementation ends at a stored physical representation.
- **Metric**: a term whose one-to-one implementation necessarily includes a calculation.
- **Generation Rule**: reusable behavior for applying knowledge while generating SQL.

Do not make one asset contain an independently meaningful binding, calculation, qualifier, and
query operation. Split them and assign one owner to each.

To separate Concept from Metric, use this order:

- Find the complete atomic Question term.
- Trace the complete Gold expression that answers that term; do not stop at a similarly named
  Column.
- If a target alone returns or tests the full term, it is a Concept.
- If Gold must aggregate or calculate from that target to produce a named quantity, the target is
  an operand and the quantity is a Metric.
- An operation used only to filter, select, group, order, or display an existing value belongs to a
  Generation Rule.
- Comparing or classifying complete stored or calculated values to produce a derived label is
  Generation Rule behavior unless one local physical target directly stores or tests the complete
  label meaning. A CASE expression or a named output alias does not make that derived label a
  Concept.

Apply the term test before the operation test. A phrase that only instructs the query to relate
complete operands is Generation Rule behavior. If the phrase names the calculated result as one
business quantity and Gold proves its formula, it is a Metric.

Words such as “score”, “average”, “rate”, or “count” do not decide the owner. Projection and pure
MIN/MAX keep an existing value; the selection behavior is Rule-owned.
Finding a same-named stored Column does not end the test. If Gold uses that Column only as an input
to the operation that produces the term, the Column is an operand, not a same-name Concept.

A literal's type does not decide whether it is reusable knowledge. A stable named entity, member,
category, period, or event may be a Concept with a complete Column–Value binding. A value supplied
only for a separate request operation is runtime data. Keep
the named field and a natural-language Rule when useful, but not the current input value. A named
entity can be a Concept even when it limits the current Metric calculation.

An ordinary report year, month, or quarter is runtime time scope. Treat a period as a Concept only
when the Question names a governed fiscal period, event, campaign, or cohort with a stable binding.

A Metric names one indivisible calculated quantity: its expression defines that quantity and its
plan implements the expression. Request field uses may explain its inputs without becoming its
definition. Request selections do not become fixed population merely because the Question names
them or Gold implements them.

Do not create a Metric when a standard aggregate request is applied to a complete Concept, entity
set, stored value, or existing Metric. Replace the operand: if the remaining words still express
the same generic operation, keep the operand's owner and use Generation Rule behavior. Thus
`average unemployment ratio` over a stored ratio is the base Concept plus an average operation,
and a compositional `number of X` is X plus `count_aggregation`. This rule does not split a named
business quantity whose formula defines a new measure, such as `retail efficiency`, `revenue growth
rate`, or a named coverage measure whose counting unit is a distinct tuple.

Apply that split from the Question's wording and the operand's meaning, not merely from an aggregate
function in Gold. When Gold only obtains the complete named business quantity by aggregating
finer-grained raw values, the raw Column is an operand and the named quantity may be a Metric. For
example, `total invoice amount` proved by `SUM(invoice_line.amount)` is Metric-owned; the raw amount
Column does not directly implement the requested total. By contrast, `average unemployment ratio`
explicitly applies a replaceable average operation to an already-complete stored ratio.

For every current-case detail, choose exactly one treatment:

- **Remove current values** such as the selected entity, date, member, threshold, or K when the
  asset remains complete without them. A Metric plan may retain a filtering or grouping field when
  it clearly states that field's role in the proved case.
- **Use natural Rule prose** for supplied inputs, such as “the date specified in the Question” or
  “the requested number of results”. Do not replace the Question's business words with role
  placeholders.
- **Keep** physical binding, formula, counted object, required calculation grain/order, source
  alignment, or required NULL/zero behavior only when the term explains that content.

Do not hide field roles as “specified scope” or “relevant field”. Do not widen one Case to arbitrary
entities, measures, dimensions, or sources, or add a default or
unobserved branch. Cross-case decisions belong to Generalization.

## Naming and references

Names must come from the Question and make sense alone. Use an exact phrase by default. An
adjective, participle, or qualifier alone is not self-contained; add only the entity it clearly
modifies in the Question. Small grammar changes are allowed. Keep words that distinguish the
requested physical representation or entity; removing them would make the term broader. Do not add
synonyms or new business meaning.

Copy IDs from `physical_reference_catalog`. Resolve aliases and CTEs to physical sources. Apply the
necessary-reference test to the asset's own contract, not to plan prose alone. For a Metric,
ontology references are derived only from `expression.refs`: a physical `<table>` or
`<table.column>` named only in its generation plan remains plan-local. Keep that token in the plan
when the implementation needs it, but never add it to Metric `references` or its graph dependency
closure. Concept and Generation Rule references follow their own type contracts.

Use two distinct formats:

- Physical SQL and plan tokens: `<sales>` and `<sales.amount>`.
- Ontology references: `table.sales` and `column.sales.amount`.

Correct: `expression.refs.amount = "column.sales.amount"` and plan text
`Sum <sales.amount> from <sales>.` Never write `<column.sales.amount>` or `<table.sales>` in a plan.

## Output

Return exactly `<reasoning>...</reasoning>` followed by `<result>...</result>`. Put the schema JSON
object directly inside `<result>`, with no Markdown fence and no extra JSON wrapper.
