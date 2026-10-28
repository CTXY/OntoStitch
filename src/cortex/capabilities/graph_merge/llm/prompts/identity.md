# Task

Match semantic identity to the seed, not compatibility. Same identity permits one canonical ID even when consolidation must retain competing implementations. Use names/aliases, full meaning and implementation to understand what each asset denotes; concrete implementation differences alone do not settle identity. Do not extract, retype or redesign assets in this step.

# Type criteria

- Concept: the same business object, category, state, qualifier or directly stored meaning. Different physical bindings can express one meaning; shared names or a shared parent category are insufficient.
- Metric: the same business quantity. Compare names and descriptions with expressions and plans to identify the measured subject. Equal operators/formulas alone are insufficient; different formulas can still compete as implementations of the same named quantity.
- GenerationRule: the same independently reusable request-triggered SQL-generation decision. Compare requested result and obligations using condition/action; operation_intent is only a clue. Different COUNT operands can belong to one decision. Returning a maximum value and returning its record are different decisions even if both sort. Leave compatibility and conflict to consolidation.

Distinguish a different requested result from disagreement about how to produce the same result. For the same requested quantity, opposing action/rationale claims (including row versus distinct counting) are potential competing implementations, not by themselves different identities. Separate them when the request or business meaning establishes different counting subjects or results.

Independently applicable obligations remain different, even if they are complementary: for example, adding a dimension to `GROUP BY` versus aggregating a stored attribute, or applying one required filter and excluding a different unwanted filter. Co-occurrence in a query does not establish identity.

For each GenerationRule comparison, answer four questions in the reasoning: (1) Is the requested result or intermediate obligation the same? (2) Are the affected business subjects and roles the same? (3) Are these alternative or competing implementations of one obligation, rather than independent obligations that can apply together? (4) Does the difference change only implementation/request values, or the business requirement itself? Compare the complete condition/action and references with supplied source question/SQL. Patient count versus examination count and patient-to-examination versus patient-to-hospital joins are different decisions. COUNT versus COUNT DISTINCT for the same patient-count request may compete within one identity. Two ranking implementations may match; maximum value versus the record having that value does not. Different operation_intent labels must not override this comparison.

Source evidence excerpts may be bounded or unavailable. Use actual question/SQL meaning, never source IDs, as evidence. Missing or truncated evidence does not establish sameness; preserve separate identities when uncertain. Retrieval similarity and matching physical references are only reasons to compare, never proof of identity.

# Comparison scope

Every verdict compares the named candidate to the seed in decision_anchor, not to another candidate. collection_candidates are existing groups; select at most one best same-identity group. This selection does not merge existing groups together. query_candidates are ungrouped peers, each compared separately to the seed. Several peers may match. Source/case IDs are provenance, never identity evidence.

# Output

Follow the supplied schema. selected_entry_id must be an exact supplied entry_id, or null when no unique group is established. Return one query_verdict per supplied query_id. If sameness is uncertain, leave the existing group unselected and return different for that peer. Give concise reasons.
