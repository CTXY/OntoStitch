For a Concept with `targets`, compare target business semantics rather than serialization details.
Target IDs, filter IDs, object ordering, and presentation-only SQL differences do not create a new
target when relation, canonical semantic target, binding behavior, and business filter are equal.
Merge equivalent descriptions into one canonical description. Preserve genuinely different but
compatible source-scoped targets. Return `unable` for contradictory mappings.
