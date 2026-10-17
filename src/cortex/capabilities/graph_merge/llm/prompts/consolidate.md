# Task

Generalize one matched semantic identity into reusable complete content. Jointly revise the common
expression and the explanation of remaining requirements; do not freeze a shared/residual partition.

# Input

The group contains original definitions and source Questions/gold SQL. dependency_evidence and its
conflict alternatives supply referenced meanings. effective_fields defines allowed content fields.
request_scope_candidates are derived hints, not proof. working_memory supplies the previous proposal
and specific revision feedback. Use originals as evidence, never an earlier proposal as ground truth.

# Procedure

1. Understand each source's actual behavior, concrete operands and inherited dependencies. Distinguish
   request inputs from requirements owned by the node. Repeated values are not automatically constants.
2. Propose a reusable definition: a common operation with meaningful request inputs, and compatible
   behavior under justified business conditions. Shared knowledge need not be a textual intersection.
   Unobserved request values are allowed when induction follows from known meaning and definitions;
   absence of counterexamples alone does not justify new behavior.
3. Revisit every source. Incorporate remaining requirements into the common definition or compatible
   conditional content. Revise the abstraction if needed. Keep complete competing definitions only
   when overlapping requests can require different results and no justified selector resolves them.
4. Write complete candidates using the type requirements below. Inputs, conditions and dependencies
   must appear in usable content, not merely in reason. Address feedback by revising content,
   interpretation or grouping; retain earlier correct repairs.

# Preservation boundary

Remove incidental request restrictions and repeated implementations, not result-changing obligations.
An invented generic role or unspecified external operation cannot replace a required computation.
Delegation is valid only when the candidate or a supplied dependency makes the source behavior
reconstructible without guessing. Explain meaningful inputs naturally; do not create a parameter DSL.
Use business conditions, never case/source IDs or implementation introspection, to select behavior.
Distinguish the semantic role of context from the required behavior. Values explicitly supplied by
requests in the same input role can be generalized while keeping the quantity and operation fixed;
a value appearing in an original name is not proof that it is intrinsic. For DIFFERENT behaviors,
identify what context determines the difference and why that context can serve as a condition.
Justify each branch, including any otherwise/default branch: proving one branch does not justify
its complement, and the absence of a context is not evidence for a different required behavior.
Gold SQL establishes the behavior to preserve, not that a surrounding feature requires it. Filtering
scope and grouping grain decide which records/groups to use; without independent semantic grounds,
they do not decide the counting unit or aggregation meaning. Do not merge such choices merely by
branching on scope/grain or renaming identity to "A or B". Explain the link briefly in reason; no role
taxonomy or new fields. When the link is missing, narrow the claim, preserve evidenced overlapping
competition as conflict, or insufficient evidence as unresolved. New input values need no new cases.
Expand dependencies, including alternatives; a conflict envelope is not a unique definition. Retain
only references the generalized content needs, using supplied IDs. Correct demonstrably false prose
without changing evidenced behavior; uncertainty calls for preservation, not an invented conflict.

# Output

Follow the supplied schema. Each input ref belongs exactly once to a complete candidate or
unresolved_refs; the program restores unresolved originals. A candidate can combine common behavior
and compatible specifics. Multiple candidates must represent competing positions, not case copies.
Publish difference_points for those unresolved business choices, linking positions to all resolved
members and their exact source case IDs. Keep opposing positions in separate candidates and do not
split identical position combinations. Unresolved members do not enter difference_points.
Give a short reason for the reusable expression and necessary differences. Returning all originals
unresolved is allowed when no justified construction is available; it is not generalization success.
