# Extraction failure learning

An observed generated-SQL error is learning evidence. A readable or locally valid Rule is insufficient when the required executable behavior remains wrong. Correct bindings and unrelated knowledge remain valid; the controller must not manufacture business facts to explain an implementation error. Infrastructure failures and explicitly isolated contradictory training records are handled separately.

## Responsibilities

- **Diagnosis:** inspect the latest failed SQL, execution/result evidence and the exact candidate graph that produced it. Identify minimal reusable corrections. Do not explain an operator, filter, scope or tie policy that is absent from the actual SQL.
- **Learning:** correct the responsible binding/formula, strengthen the minimal Rule condition/action or implementation pattern, or add an independent reusable Rule. Do not dismiss execution failures as solely a generator responsibility. Avoid redundant Rules and generic instructions to generate valid SQL.
- **Assessment:** evaluate definition quality, observed application and fixed-target resolution separately across all supplied attempts. A correct function or reasonable definition does not prove complete output behavior. Preserve unrelated valid units; give actionable feedback for remaining failures. Old targets can be resolved while another SQL error still requires Diagnosis.
- **Controller:** accept only the exact candidate supported by stable-correct replay and Assessment. If fixed targets are resolved but SQL still fails, start the next outer Diagnosis on that failed candidate. If a repair makes no change while SQL remains wrong, end that inner repair and advance to the next outer Diagnosis instead of terminating the case. Both transitions consume the configured outer-round budget.

Gold defines required behavior, not mandatory function names. SQL patterns illustrate implementations. Diagnosis explanations are hypotheses to check against SQL and evidence, not axioms that justify permanent prohibitions. Assessment receives actual attempts and fixed targets before detailed unit inventories. The operation registry includes complete alternatives and their population/NULL prerequisites; mathematical equivalence must not be rejected merely because a different SQL function is used.

## Bounds and persistence

Inner and outer round limits remain configuration-driven; this experiment uses five each and no model-call cap. Re-diagnosis does not reset outer rounds or accept tentative assets. The accepted baseline remains unchanged until both gates pass. The failed working snapshot remains available as the next Diagnosis input, avoiding an unnecessary empty-baseline SQL replay. Changed workflow/prompt contracts require a fresh case run; incompatible old checkpoints are not resumed as if equivalent.

## Physical JOIN normalization

Merge uses the existing physical JOIN identity: table endpoints and complete equality-column pairs, independent of direction. Duplicate physical joins reuse one edge and union provenance, checking cardinality in the proper direction. Semantic property/branch variants are not silently collapsed. The same normalization applies when resuming a legacy uncommitted merge checkpoint, with edge aliases recorded in lineage.
