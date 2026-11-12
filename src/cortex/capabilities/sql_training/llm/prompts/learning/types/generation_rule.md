# Generation Rule

{rule_atomicity}

## Properties

- `condition`: all prerequisites; linked business names may clarify scope.
- `action`: minimal complete behavior; explain operands, placeholders and any local SQL pattern.
- `reason`: case evidence and the consequential implementation choice; extensions follow the shared registry contract.
- `triggers`: retrieval signals, not prerequisites. Direct phrases occur contiguously in the Question and SQL signals stay empty; Repair may use actual errors.

## Reuse and scope

Rebinding business meanings requires checking applicability. Author applies_to edges explicitly; no general references field. Group selection does not request member details; nullable output does not require NULL filtering.

{count_semantics_rule}

## Complete node example

Illustrative; use case evidence.

```json
{"id":"generation_rule.select_ranked_groups","node_type":"generation_rule","name":"Select ranked groups","properties":{"operation_intents":["top_k_selection"],"condition":"The request asks for keys of leading or trailing groups under a specified measure.","action":"Rank groups by the requested measure and direction, retain the requested K group keys and preserve the specified tie policy. Count only when the measure is a count.","reason":"Group selection retains the key; extrema of stored values do not imply counting.","triggers":{"question":{"phrases":["leading"]},"sql":{"columns":[],"operations":[]}}}}
```
