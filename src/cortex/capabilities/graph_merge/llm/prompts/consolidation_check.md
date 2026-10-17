# Task

Independently assess a COMPLETE proposal against all supplied original group members, sources and
dependencies. Check justified generalization AND preservation of required behavior. The proposer’s
reason is a claim to test. The scope is supplied evidence, not an open-world absence-of-conflict proof.

# Input

proposal contains complete candidates, competing positions and unresolved refs. The original group
and evidence_by_ref remain available even for members outside a candidate. dependency_evidence and
its alternatives explain inherited behavior. effective_fields defines allowed content. Derived
request_scope_candidates are hints to verify against sources. working_memory contains only run state,
historical points and any format-repair feedback, not prior semantic verdicts to follow.

# Checks

1. Generalization: are the common operations, variable inputs and conditions justified by request
   meanings, definitions and dependencies? Different operands are not interchangeable merely because
   they use one operator. Repeated case values are not necessarily intrinsic constants. Permit
   justified induction to unobserved values, but reject arbitrary new behavior. Also identify concrete
   unnecessary restrictions or duplicated implementations; do not demand maximum possible breadth.
   Distinguish context role from behavior: explicit request values in the same input role may vary
   while the quantity and operation stay fixed, even if old names contain those values. For a condition
   resolving DIFFERENT behaviors, require a semantic link from context to the required difference.
   Check EACH branch, including otherwise/default: proof for one branch does not justify its
   complement. A missing link in any behavior-changing branch requires revise or unknown.
   Gold SQL proves what happened, not that a surrounding feature requires it. If the condition only
   gives filtering scope/grouping grain but the branch changes counting unit or aggregation meaning
   without independent grounds, generalization MUST be revise or unknown, even if all sources are
   covered. Neither matching SQL patterns nor an "A or B" identity supplies those grounds. Explain the
   context's role and the missing/valid link briefly in generalization.reason. Do not invent evidence,
   require cases for every new value, or turn uncertainty alone into conflict.
2. Preservation: for every assigned source, first test whether its context selects the claimed
   behavior, then whether its required calculation, conditions, output and dependencies survive.
   Common content and compatible specifics can jointly preserve a source; full SQL need not be copied.
   Apply the type requirements below. A requirement is not preserved by renaming it or declaring an
   unspecified external calculation responsible. The candidate or supplied dependencies must make
   the source behavior reconstructible without guessing a missing operation.
3. Organization: does the complete proposal account for every original and handle known contradictions?
   A candidate need not cover another explicit competing position, but that position must have complete
   content and a justified difference point. Competition requires overlapping scope, possibly different
   results and no justified selector. Uncertainty alone does not establish conflict. preserved_refs
   are unchanged, explicitly unresolved originals: they need no new coverage proof but remain possible
   counterevidence to NEW candidates. Do not fail merely because preserved relationships are unknown.

# Judgments and feedback

- generalization: acceptable if justified and no concrete supported de-specialization remains;
  revise for a false abstraction or avoidable restriction; unknown when justification is unclear.
  A revise reason must identify the problem and a feasible correction. not_applicable is only for
  Concepts without a generalization need or all-original preservation, not new Metric/Rule content.
- consistency: acceptable if source allocation and competing content form a coherent complete scheme;
  revise for an organizational defect or unhandled known contradiction; unknown for uncertainty in
  new content. Put source-specific behavioral evidence in the coverage rows rather than repeating
  full arguments in both judgments.
- Feedback should name the affected behavior and evidence: candidate_defect, unnecessary_specialization,
  unsupported_condition, dependency_misinterpretation, unresolved_competition or insufficient_evidence.
  Do not prescribe the replacement grouping. Missing examples alone are not counterexamples.

# Output contract

Follow the supplied schema. Return exactly one verdict for every representation_checklist entry,
one source verdict per source_checklist pair, and one plan verdict per plan_checklist pair. Do not
add coverage entries for preserved_refs. Each source row separately tests condition_verdict and
behavior verdict; both must be covered for acceptance. canonical_plan_id must exist in the candidate
assigned to that supporting_ref; IDs need not be unique across candidates. No source plans means no
plan verdicts. Evidence SQL is not itself a plan object.

A not_covered representation/plan verdict must cite supplied EvidenceItem IDs; counterexamples can
come from ANY supplied group source. Use unknown for missing proof, never an invented counterexample.
Keep each ledger reason to one concise sentence and explain each main issue once. Do not add fields
outside the schema. The shared output formatter specifies serialization.
