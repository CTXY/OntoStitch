# Generation Rule

{rule_atomicity}

## Properties

- `condition`: prerequisites and linked business scope.
- `action`: one complete behavior with operands and a local SQL pattern.
- `reason`: evidence, implementation choice and any registry gap.
- `triggers`: retrieval signals. Direct uses contiguous Question phrases and empty SQL signals; Repair may use actual errors.

## Scope

Recheck scope when rebinding. Author applies_to explicitly; no refs field. Group selection does not request member details; nullable output alone needs no NULL filter.

{count_semantics_rule}

## Complete node example

```json
{"id":"generation_rule.select_ranked_groups","node_type":"generation_rule","name":"Select ranked groups","properties":{"operation_intents":["top_k_selection"],"condition":"Return leading group keys by a requested measure.","action":"Rank groups by <measure>/<direction>; retain K keys with the requested tie policy.","reason":"Keep group keys; scalar extrema do not select their rows.","triggers":{"question":{"phrases":["leading"]},"sql":{"columns":[],"operations":[]}}}}
```
