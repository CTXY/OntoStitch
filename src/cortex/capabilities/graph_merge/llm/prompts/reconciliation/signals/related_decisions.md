# related_decisions: Rules share affected objects

## Trigger, cause, and normal state
The applies_to of multiple GenerationRules share objects, which may involve object or grain mismatch; it does not prove the same identity, simultaneous triggering, or behavioral conflict.

## General example (illustrative, not this round's graph records or evidence)
A Rule that ranks after aggregating by customer must not be incorrectly attached to order lines; deduplication and ranking can independently share a target.

## Necessary local material
The nominated Rules, their relevant object/input/scope relationships, and the meanings of the referenced objects. Sharing an object does not justify loading that object's unrelated neighborhood.

## Writer judgment and modification
Compare each Rule's complete conditions, behavior, and all affected objects. You may fix endpoints, scope, and related node references/descriptions, and supplement missing calculations or business objects with clear sources; do not splice independent behaviors, do not reselect Metric definitions, and do not merge Rule identities merely because objects are shared.

## Assessment check points
Check each Rule's conditions, behavior, rationale, grain, and all operands, confirming no new obligations were introduced; retention of a common target does not mean all knowledge was retained.
