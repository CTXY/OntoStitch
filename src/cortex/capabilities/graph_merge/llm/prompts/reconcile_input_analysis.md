# Role

Record what each input node says before deciding whether any nodes agree. Each member includes its
own source Questions. Do not reconcile, merge, or label conflicts. Copy every input ref once.

# For each node

- `requested_result`: what the source Question asks to receive, including whether it is an object,
  numeric value, set of rows, or detail about a selected object. Replace a concrete object name
  with its business role only when the requested result stays the same.
- `action`: the complete behavior, including operation, selected object, returned result, row count,
  ordering, and output shape when these are required by the node. Describe what its exact operands
  do; do not replace different operands with one generic role.
- `exact_operands`: copy only the fields, references, constants, and fixed predicates that define
  the node's result-producing Action. Keep the exact supplied spelling. A column merely available
  for grouping, filtering, or display is not an operand unless the node requires its use in the
  calculation or selection. For a Metric, distinguish its expression and fixed filters from
  optional dimension references. Do not invent a missing operand.

Then partition refs into `requested_results.slots`. Nodes belong to the same slot when they answer
the same abstract atomic request, even if their Actions differ or their Questions use different
words for the same business measure. A filter, selected brand, concrete object, grouping dimension,
or time range does not by itself create a new result slot; it may still change the Action or its
applicability. For example, “number of shipped items” and “shipped item count” ask for the same
result. Different output kinds or different measures are different slots even when they share
trigger words. Do not combine independent questions into a compound request. Every ref must appear
in exactly one slot.

If `validation_feedback` reports missing, repeated, or unknown refs, correct the coverage using
the exact supplied refs. Do not infer a missing node's requested result from another node; read
its own condition and source Questions.

Example: One node totals `billed_amount`; another totals `paid_amount`. Both may answer the same
request for a total, so they can share a requested-result slot. Their exact operands remain
different in the per-node records; do not decide here whether the totals agree.

Example: One node returns the product with the highest score; another returns only the highest
score value. Their Actions may both sort descending, but their requested results occupy different
slots.
