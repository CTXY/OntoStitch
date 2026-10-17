# Concept analysis

A Concept represents one business object, category, state, or qualifier and its semantic groundings.

Compare in this order:

1. `description`: identify the complete shared business meaning and any incompatible interpretation.
2. `properties.targets`: for each target, identify what value it contributes and in which table or
   Schema context it is valid.
3. Other definition fields and `references`: decide whether they add compatible meaning or change
   the Concept.

Different physical targets are compatible only when downstream planning can explain which target to
use from the selected table, Schema, query structure, or an explicit mapping condition. If two
targets can apply in the same indistinguishable context and may produce different meanings, classify
the target difference as `conflict`. Never infer compatibility from target count alone.
