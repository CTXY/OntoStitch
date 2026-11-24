# value_concepts: dimension concepts and generalization opportunities

## Trigger, cause, and normal state
Multiple Concepts bind different DimensionValues to the same column; prioritize checking for missing business dimensions or generalizable structure, and do not assume these nodes are wrong by default.

## General example (illustrative, not this round's graph records or evidence)
A Gender dimension can represent a SEX column, with Female/Male value concepts associated with Gender; FemalePatient, however, is a subset of patients, not an attribute value.

## Necessary local material
The nominated column and values, their directly bound Concepts, and relevant dimension/value, hierarchy, or attribute relationships among these objects. Inspect the dimension opportunity without loading every business relationship of each value Concept.

## Writer judgment and modification
Reuse or add dimension Concepts with clear business meaning, and connect semantic values using concrete contracts; when there is complete evidence, add a common object concept and is_a. You may clarify names, definitions, and targets, and retain classification conditions. Physical value_of does not replace semantic relationships; do not create empty shells for every column, do not treat values as object subclasses, do not guess that F/M are exhaustive, and do not create new grounding contests.

## Assessment check points
Check the hierarchy, complete matching, Value/Column attribution, and the basis for newly added claims. An existing complete structure can be kept; evidenced missing dimensions should be filled in, but unjustified parent classes or coverage/exclusivity assertions are not required.
