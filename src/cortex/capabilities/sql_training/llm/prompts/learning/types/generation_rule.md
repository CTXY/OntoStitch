# Generation Rule

Apply shared Rule admission and action minimality. Static term matching belongs in Concept; defining calculations in Metric. Multiple targets alone need no Rule.

Content selection changes represented fields/groups/objects; presentation holds content fixed and changes format/order; row filtering changes the population. Split only independently applicable complete decisions. Do not hide binding choices inside ranking/formatting or infer primary roles from field suffixes.

## Properties

- condition: supported prerequisites and linked business scope.
- action: one implementable behavior with operands and a local SQL pattern when useful.
- reason: evidence, consequential choice and any registry gap; never invent alternatives/error history.
- operation_intents: reuse fitting registry labels; multiple labels and meaningful new labels are allowed. Extensions explain meaning, gap and evidenced consequential alternatives. Registry membership is not admission.
- triggers: Direct uses contiguous Question phrases and empty SQL-side signals. Repair may use actual observed error signals. Ordinary NULL pass-through or an absent filter needs no Rule.

Preserve supported direction, operands, grain, NULL/tie semantics and intrinsic constants; parameterize incidental request values and K. An arbitrary maximum does not imply COUNT. Optional prerequisites must match the behavior. The supplied schema defines exact fields.

## Complete node example

```json
{"id":"generation_rule.select_ranked_groups","node_type":"generation_rule","name":"Select ranked groups","properties":{"operation_intents":["top_k_selection"],"condition":"Return leading group keys by a requested measure.","action":"Rank groups by <measure>/<direction>; retain K keys with the requested tie policy.","reason":"Keep group keys; scalar extrema do not select their rows.","triggers":{"question":{"phrases":["leading"]},"sql":{"columns":[],"operations":[]}}}}
```
