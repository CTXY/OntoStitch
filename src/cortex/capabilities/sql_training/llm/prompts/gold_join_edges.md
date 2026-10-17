# Task

Read the supplied Gold SQL and propose every physical, one-hop relationship between two real
database tables that the SQL structure itself proves. This is physical evidence extraction before
ontology training. Do not infer a relationship from table or column names, foreign keys, Schema, or
the question.

# Output rules

- Return one `joins` item for each direct table-pair relationship proved by an equality between
  real columns. A path A–R–B is two items, never an A–B shortcut.
- Eligible evidence includes explicit `JOIN ... ON`, `WHERE` column equality, `IN (SELECT ...)`,
  and `EXISTS` or correlated subquery equality. For `A.col IN (SELECT B.col FROM B ...)`, output
  the relationship as `A.col = B.col`.
- Resolve SQL aliases to the real names in `table_columns`. Use real unqualified column names in
  `column_pairs`. Preserve every equality needed for that table-pair relationship as one complete
  composite join. Preserve the SQL join type for explicit joins; otherwise use `INNER`.
- Omit a relationship when either endpoint is a CTE or unknown table, or when the relationship uses
  non-equality, expressions, OR alternatives, filters, or unresolved columns. Do not return a
  partial relationship.
- Omit self-relationships: never output a `joins` item when `from_table` and `to_table`
  resolve to the same real table, even if the SQL compares two aliases, an outer query and a
  subquery, or two columns from that table.
- Same-table comparisons are filters or intra-table selection evidence, not physical
  Table-to-Table join evidence. For example,
  `schools.County = (SELECT schools.County FROM schools ...)` must not produce
  `schools -> schools`.
- If the only apparent relationship is same-table, return `joins=[]`.
- When possible, set `sql_construct_type` to `explicit_join_on`, `where_equality`,
  `in_subquery`, or `exists_subquery`, and set `evidence_fragment` to the shortest SQL fragment
  that proves the relationship.
- Do not add explanatory text to the JSON output.

Return JSON matching:

{template_output}
