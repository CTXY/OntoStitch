# Task

Compare the two nodes as possible rules for one identity. Decide whether their contexts overlap and
whether their complete Actions can produce different results. Do not merge or rewrite the nodes.

# Method

1. Describe each Action literally before abstracting it. Include its concrete input or operand,
   operation, selected object, returned result, cardinality, ordering, and output shape.
2. Context is the requested business result, selected business object, and facts known before the
   Action starts. Contexts overlap only when both rules are candidates for the same result of one
   atomic request. Do not create overlap by joining two independent questions or requested results
   into a compound request. Treat different requested result slots as different contexts: for
   example, the object that has the maximum and the numeric maximum itself are different requested
   results. A general context overlaps a concrete case that it covers. Two concrete contexts are
   `parallel` when they select different concrete business objects but have the same abstract
   request shape after replacing those objects with one role.
3. Actions are equivalent only when supplied evidence proves the same final result for every valid
   input in the overlap. A common operation or role name is not proof.
4. Actions are different when one valid input in the overlap can change the result. State that input
   in `witness`; it need not occur in the supplied examples.
5. Concrete operands are part of Actions. Do not replace them with a shared role unless evidence
   proves the result is preserved. For example, counts over two fields can differ when their NULL
   patterns differ, even when both fields are called identifiers.
6. A selector is a business fact known before the Action that chooses exactly one Action. A Table,
   Column, SQL choice, source case, available field, operand, or predicate inside the Action is not
   a selector.
7. Overlap plus different Actions plus no proved selector is `conflict`. A proved selector makes it
   `conditional`. Proved non-overlap is `disjoint`. A concrete rule is a `refinement` only when the
   general rule leaves the compared behavior unspecified. Parallel concrete contexts with
   equivalent parameterized Actions are `generalize`, not disjoint.
8. For general-and-concrete contexts, compare Actions only inside the concrete intersection.
   Instantiate the general rule's parameters from that concrete request. Behavior for another
   object or measure outside the intersection cannot be a witness.
9. Compare final results, not incidental SQL shape. A subquery and a direct ordering are equivalent
   when they return the same requested result for every input in the overlap. They differ when
   cardinality, ties, NULL handling, ordering, selected object, or returned values can change.
   Treat a grouping, ordering, or output shape as part of the Action only when the node contract
   requires it. In a metric, an expression and metric-local filters define the calculation; a SQL
   plan that merely illustrates grouping by a caller-requested dimension does not make that grouping
   a fixed metric behavior. Do not infer a mandatory shape from an example plan.
   For a Metric, treat its expression, references, and explicitly intrinsic calculation prose as
   the semantic contract. Current-case SQL plan examples are implementation guidance, not a source
   of fixed grouping or filtering behavior.

# Output checks

- Copy the two input refs exactly.
- `equivalent`: equivalent Action, no selector.
- `generalize`: parallel concrete contexts and equivalent Actions after replacing the concrete
  objects with the same business role.
- `conditional`: different Actions, proved selector.
- `conflict`: different Actions, no selector.
- `selector` is non-null only when proved.
- Case IDs and implementation choices are provenance, not context boundaries.

# Examples

Example 1 — general and concrete:

- Node A selects the entity with the highest value of the requested measure.
- Node B selects the school with the highest enrollment.
- Analysis: inside B's context, A's entity is school and A's measure is enrollment. If both return
  the same requested school under the same tie policy, B is covered by A; do not use another measure
  outside B's context as a witness.

Example 2 — disjoint atomic requests:

- Node A returns the school that has the highest score.
- Node B returns the highest score value.
- Analysis: one atomic request asks for an object and the other asks for a number. They are disjoint
  even though both mention the same score and use ordering. Do not turn that difference in requested
  result into an Action conflict.

Example 3 — parallel concrete cases:

- Node A computes reorder rate for sofas as sales quantity times 30 divided by display days.
- Node B computes reorder rate for beds with the same operands, formula, grouping, and NULL behavior
  inside the selected product range.
- Analysis: the product objects differ, but replacing them with `selected product range` gives the
  same context and Action. Use `parallel` and `generalize`, not `disjoint`.
