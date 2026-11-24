# Guidance for This Type Group: Metric

## 1. Integration goal

Integrate the complete calculation definition of the same business quantity. Preserve formulas, operand meanings, population, units, grain, `NULL`/zero-value behavior, and calculation order already committed by the sources; similar names do not prove equivalent calculations.

## 2. Field-filling rules

- **`properties.expression`**: required object, containing only `expr` and `refs`. `expr` is a non-empty calculation expression, not a full SELECT; the set of `${key}` placeholders must exactly match the keys of refs, and refs map keys to real node IDs.
- **Calculation validation**: bare columns, pure CAST, and identity expressions wrapped only with `+0`, `-0`, `*1`, or `/1` will be rejected. When historical input does not conform to the contract, report the problem; do not fabricate operations such as SUM to pass validation.
- **`properties.pattern`**: optional calculation description, synonymous with the formula, and cannot replace operations or conditions missing from the formula. Necessary meanings such as units and grain go in existing description fields; do not add undeclared attributes.
- **`properties.sql_generation_plans`**: optional list; each item contains only id, application, steps. id matches `[A-Za-z][A-Za-z0-9_]{0,99}` and is unique; application is non-empty text, at most 500 characters after trimming leading and trailing whitespace; steps is 1–8 non-empty text entries, each at most 600 characters after trimming leading and trailing whitespace.

---

## 3. Merging, generalization, and conflicts

- Complementary explanations, names, and equivalent plans for the same calculation can be integrated; coexisting multiple plans must retain their respective applicability preconditions. Do not assemble formulas from different definitions or mix refs.
- Request time windows, display options, or sorting options may be generalized only when they are not part of the fixed definition; fixed population, thresholds, unit scaling, and calculations cannot be discarded. `${key}` binds a node, not an arbitrary request-parameter system.
- Preserve real operations: `SUM(price / amount)` is not automatically equal to `SUM(price) / SUM(amount)`, and you cannot change SUM to AVG just because the name contains "average". When verified definitions in the same scope are incompatible, retain their respective complete formulas and descriptions.
- Do not replace operands whose equivalence has not been confirmed. If operand identity affects whether the current formulas can be merged, report this specific precondition; do not change refs merely because neighbors might be merged in the future, and do not leave unresolved content issues as ordinary relationship matters.

### Complete content example

Assume the sources confirm the same order scope, grain, and summation formula, one provides a Chinese name and the other provides an equivalent English name; this can be written as the following `NodeContent`:

```json
{
  "name": "Total order payment amount",
  "description": "Sum the order payment amounts within the source-confirmed scope, preserving the original order grain and SUM behavior.",
  "aliases": ["Total order payments"],
  "tags": ["Payment amount"],
  "properties": {
    "expression": {"expr": "SUM(${amount})", "refs": {"amount": "column.orders.amount"}}
  }
}
```

Actual references must come from this round's input; do not use the example to add a calculation to a source that originally had no summation.
