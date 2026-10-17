# Role

You analyze a candidate SQL failure and identify reusable failed logic.

# Input

- `question`: the natural-language question
- `gold_sql`: the reference SQL
- `predicted_sql`: the SQL rejected by the database
- `execution_error`: the database execution error
- `table_schema`: the available schema
- `matched_ontology`: ontology assets matched for this request
- `draft_metrics`: the current Metric assets
- `evaluation_contract`: full result-row-set equality; every Gold output column is required, row order is ignored
- `accepted_working_assets`: the complete accepted base catalog

# Task

Identify reusable failed logic in `confirmed_errors`. Return at least one confirmed error and cover
every confirmed error exactly once in `repair_groups`.

# Confirmed Error Requirements

For every item in `confirmed_errors`:

1. Assign stable IDs in order: `E1`, `E2`, and so on.
2. Copy the input `execution_error` into the output `execution_error`.
3. Copy the smallest relevant fragment of `predicted_sql` into `failing_sql_fragment`.
4. State the expected logic and generated logic concisely.

# Repair Group Ownership

Default to one Repair Group per confirmed error. Choose the owner by the Asset that can represent
the reusable repair through its structured fields:

- `semantic_mapping`: the direct physical binding of one atomic concept, entity, qualifier,
  predicate, value, or field meaning;
- `metric`: the intrinsic calculation contract of one atomic named measured quantity;
- `generation_rule`: a conditional instruction for using or coordinating otherwise valid Assets.

Do not choose from the failing SQL clause alone. Split an error when one Asset cannot express the
complete repair. Each group must include one owner and a reusable `repair_instruction`.
When a mapping already exists, repair `semantic_mapping` only for a proven binding or
retrieval-facing content defect. If a valid mapping was retrieved but not applied, use
`generation_rule` only when a reusable condition/action can ensure its correct use. Multiple sources
internal to one named Metric remain part of that Metric's calculation contract.
An identifier invented in SQL is not by itself a missing Semantic Mapping. Choose
`semantic_mapping` only when the identifier represents a named Question concept or proves that an
accepted Mapping has the wrong physical binding. When the SQL incorrectly assumes that a column or
join key is valid across sources, route a reusable source-local schema-grounding or coordination
instruction to `generation_rule` if its condition and observable action can be stated.
Use `base_asset_id` only for an exact same-identity asset in `accepted_working_assets`; otherwise
leave it empty and create a new asset.

# Boundaries

- Focus only on the database-rejected logic and whether it is useful ontology-learning evidence.
- Do not infer an infrastructure or permission failure from the database error text alone. Compare
  every relation and column identifier in `predicted_sql` with `table_schema` and the physical
  properties of matched ontology nodes. If SQL copied an ontology Node ID such as `table.orders`
  or `column.orders.id` instead of its physical identifier, classify the invalid generated
  identifier as a `confirmed_error`; it is not a database-access difference.
- Do not classify errors by type.
- Do not propose assets.
- Do not infer requirements from formatting that appears only in `gold_sql`.

# Output

Return a JSON object matching these format instructions exactly:

{template_output}

<reasoning>
Locate the database-rejected logic that belongs in confirmed_errors. Do not put JSON in this section.
</reasoning>

<result>
Return exactly one JSON object matching the format instructions above, without Markdown fences or extra text.
</result>
