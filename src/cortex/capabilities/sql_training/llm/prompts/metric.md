# Role

Extract Metric candidates from one Question and its Gold SQL. Use the shared asset contract;
do not merge identities, infer cross-case applicability, or decide acceptance.

# Input

- `question`
- optional `evidence`
- `sql`
- `database_dialect`; `sql` has already been normalized in this same dialect
- `gold_column_metadata`
- `database_context`
- `physical_reference_catalog`
- `current_metrics`
- optional `prior_case_metric_candidates` and `extraction_self_check` on the second pass

# Metric contract

The Question supplies business terms and separately requested operations. Gold is authoritative for
formula, operation, grain, and lineage. Optional evidence explains that implementation but cannot
create, rename, or remove Question terms. Schema and gold_column_metadata establish physical
bindings and field meanings. Existing assets supply reusable identities, not what this Question asks.

Apply the shared Metric contract to the Question and complete Gold lineage.
Find the smallest Question terms first. Split out a term that separately matches an entity, stored
meaning, existing Metric, or requested operation. Keep a complete term as well only when it needs
its own calculation. If a phrase only asks to apply an operation to a complete entity, stored value,
or existing Metric, the combined phrase is not a Metric. Decide from the Question meaning, not the
SQL function. Do not create Metrics from unnamed formula parts.

Use a split test before creating any Metric: if the proposed Metric name can be split into smaller
parts and each part has its own matching SQL fragment, the whole phrase is not one atomic Metric.
The part that names an entity, member, category, stored value, or existing Metric keeps that owner;
the part that names a requested operation stays as query behavior. In particular, `number of X` or
`count of X` is not a Metric when X is already a complete entity set, Concept, stored value, or
Metric and Gold implements the remaining words as `COUNT(X)`. Create a Metric only when the
complete phrase names a new business quantity whose calculation cannot be reduced to a generic
operation applied to an already-complete operand.

# Expression

- Interpret every Gold function, operator, quote, cast, and date expression in
  `database_dialect`. Do not reinterpret MySQL Gold as SQLite or SQLite Gold as MySQL.
- Trace Gold through aliases, CTEs, and subqueries.
- `expression.expr` is the complete calculation formula for this Metric only. Do not include other
  Question concepts, filters, grouping, ranking, ordering, or display instructions.
- `expression.refs` contains only the Metrics and Columns used by that formula. A Column is its
  stored value; a placeholder name cannot turn it into a sum, count, or other calculation.
- Read the expression with every Column placeholder replaced by that stored value. If a required
  calculation then disappears, write the calculation in `expr` instead of naming the placeholder
  as though it were already calculated.
- Keep formula order, fixed constants, required grain/population predicates, and important
  NULL/zero behavior. The expression must implement every word retained in the Metric name.
- Preserve every Gold function, cast, date conversion, operator, and operation order used by the
  Metric formula. Plan prose cannot replace any part of the expression.
- Preserve Gold's exact counted object and aggregate modifier. `COUNT(x)` and
  `COUNT(DISTINCT x)` are different formulas; do not add DISTINCT from a uniqueness assumption or
  possible join duplication.
- Reuse an exact `current_metrics` dependency when available.
- Candidate sets must be closed and acyclic.

# Pattern and generation plan

`pattern` is a short readable statement of the expression. It must not summarize the current query.

The plan must have a two-way match with the Metric:

- Every expression operation appears in the plan.
- Every calculation step implements the term and expression. Request-specific field roles may
  explain how records enter or are grouped for that calculation; they do not add formula semantics.
- Explain the calculation from source to result using business operations, not identifier lists.
  Preserve source calculations, correspondence, cardinality, and NULL behavior. If a current
  filter or grouping field is useful to explain, put its role where it acts, omit its value and
  predicate, and do not make it part of every calculation. Omit the role when it would imply the
  predicate or a mandatory grouping.
- Exclude full SQL and temporary aliases.
- Use physical `<table>` and fully qualified `<table.column>` tokens. Never use `<column>` or a bare
  physical column name in plan text.
- Do not copy a child Metric's physical references into the parent plan. Use
  `metric.<asset name>` only in `expression.refs`; call the child by its business name in plan
  prose. Code derives each asset's direct reference closure.

# Candidate sets

- Use one set for each independent Question-facing root.
- Enumerate every independently named quantity term before building sets; do not omit one because
  another Metric or the final SQL output already covers part of the Question.
- Include only new Metric dependencies required by that root.
- Combine roots only when a shared new dependency makes them one connected set.
- Metrics merely requested or projected together stay in separate sets. Shared filters, grouping
  fields, source tables, CTEs, or final result coordination do not connect their formulas.
- Do not output member IDs or root/dependency labels. Code derives those roles from exact
  `metric.*` references among the set members.
- Do not return asset IDs, `aliases`, `references`, lifecycle fields, raw Columns, Concepts,
  qualifiers, or query operations. Code derives IDs and aliases from names and derives references
  only from `expression.refs`; physical plan tokens remain local to that plan.

# Short examples

- `number of verified members`: `verified members` is the entity and `number of` is COUNT. Do not
  create a Metric for the combined phrase.
- `number of value-for-money gas stations`: when `value-for-money gas stations` is a complete
  Concept and Gold only applies `COUNT(gas_station_id)`, do not create a Metric for the combined
  phrase; keep the Concept and leave COUNT as query behavior.
- `revenue`: if Gold uses `SUM(amount)`, write `SUM(${amount})`. `${total_revenue}` bound to the raw
  amount Column is not a sum.
- `return rate` for a selected region: put the rate formula in expression. The plan may say the real
  region field selects inputs, without stating its value or predicate.
- For `SUM(${used}) / SUM(${available})` from two sources, the plan sums each input, matches the
  results through real key columns, then divides.

# Output

When `extraction_self_check` is present, use exactly the same rules to inspect the Question again.
Return only omitted Metric terms. Do not repeat or revise a Metric in
`prior_case_metric_candidates`; return an empty `candidate_sets` list when none is missing.

Return `candidate_sets` matching:

{template_output}

In `<reasoning>`, briefly list the smallest Question terms and any smaller entities, stored meanings,
Metrics, or requested operations split from them. Check that each expression contains only its
Metric formula, every Column ref is used as its stored value, and the plan implements that formula.
For a phrase that applies an operation, state whether its input is already a complete entity, stored
value, or Metric, or whether the complete phrase names a calculation of its own.
Verify connected formula-dependency sets.
Return `<reasoning>...</reasoning>` followed by `<result>...</result>`, with the JSON object directly
inside `<result>`, without Markdown fences or an additional envelope.
