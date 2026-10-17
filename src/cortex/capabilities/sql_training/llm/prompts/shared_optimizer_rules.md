# Shared Optimizer Rules

1. Owner, operation, base identity, requirement, Targets, and Candidate member IDs are frozen.
2. Return complete reusable asset content, not a patch, route, or pipeline action.
3. Use only supplied exact reference IDs and keep only necessary references.
4. CREATE does not generate aliases; UPDATE preserves existing aliases without inventing new ones.
5. Validation feedback may improve how the asset is retrieved, selected, or applied, but never
   changes the frozen task or Gold-proved behavior. If feedback conflicts with the frozen
   requirement, preserve the frozen requirement and ignore the conflicting instruction.
6. `working_ontology` is the complete ontology view visible to this Optimizer. It contains accepted
   Working assets and may also contain Candidates produced earlier in the same Heavy Round. Reuse
   every exact ID and its supplied content. Do not recreate an existing identity or copy a visible
   asset into `dependency_assets`. Visibility does not mean that a same-round Candidate has already
   been accepted; acceptance remains a later Pipeline decision and is not this Optimizer's task.
7. Follow the injected JSON Schema. It defines the only asset type and fields this frozen owner may
   return. Omit `id` for CREATE; preserve the supplied ID for UPDATE and refinement. Never output
   provenance or another owner type.
8. A resolved SQL Target proves that the required behavior can repair the Case; it does not prove
   every causal statement or broader applicability claim in the Candidate.
9. Omit values supplied solely for the current request and temporary SQL names while preserving
   formula-owned predicates and constants, proved business inputs, and their uses. Do not widen
   one role into an arbitrary dimension, field, source, or operand.
10. Reason stops at the action's first result; a later consumer's NULL conversion, aggregate,
    formula, or presentation effect belongs to that later operation.
