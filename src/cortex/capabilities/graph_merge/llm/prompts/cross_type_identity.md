# Task

Resolve a possible Concept/Metric extraction type error for the same or aliased term. Inspect complete Question meanings, definitions and implementations, not name overlap or numeric datatype alone.

A Concept directly binds an atomic stored meaning, including a numeric value. A Metric defines a named quantity whose meaning requires its own calculation. Applying a general query aggregation to an already complete stored meaning does not automatically create a Metric; an intrinsic formula can still use a stored Column as an operand. Do not preserve an operand-only Concept that claims the same computed meaning, but keep an independently named stored operand when it denotes a different thing.

# Output

Follow the supplied schema and return one decision per pair_id. Return different with keep_ref=null when both assets are valid and distinct. Return same_object only when both claim the same meaning and one is mistyped; set keep_ref to the supplied, correctly typed and complete node. If correctness or semantic identity is uncertain, return different rather than deleting an unproved duplicate.
