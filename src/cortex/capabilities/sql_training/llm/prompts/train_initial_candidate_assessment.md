# Role

Assess Initial Concept, Metric, and Generation Rule Candidates after the Induction replay.
For each Candidate, judge relevance, usage, and intrinsic quality independently. Apply the same
Question-term boundary consistently when several Candidates claim that term.

Do not repair assets, change ownership, decide acceptance, or judge Candidate-caused regression.
There is no Baseline replay in this stage.

# Required response format

1. Write `<reasoning>` with a short relevance, usage, and quality check for each Candidate.
2. Then write `<result>` containing exactly one schema-valid JSON object.

Do not return bare JSON, Markdown fences, or another wrapper. Resolve revisions in `<reasoning>`;
write every JSON field exactly once.

# Input schema

## `case_evidence`

- `question`: business terms and requested behavior.
- `gold_sql`: correct implementation for this case.
- `evidence`: optional help for understanding Gold; it does not override Question or Gold.
- `gold_column_metadata`: meaning of Gold-used Columns; it does not prove other Columns relevant.
- `current_sql`: SQL generated with all Initial Candidates. It may contain unrelated errors.

## `assessment_units`

This is the complete Candidate inventory. Each unit contains everything needed to assess one
Candidate composition:

- `candidate_id`: return unchanged.
- `candidate.assets`: one Concept, one Generation Rule, or a connected Metric set.
- `candidate.root_asset_ids`: members that directly answer the Question.
- `candidate.dependency_asset_ids`: new Metrics needed to calculate a root.
- `retrieval_status`: deterministic `not_retrieved`, `not_selected`, or `selected` for all required
  roots. Never override it. A Metric dependency need not be selected independently.

`validation_feedback`, when present, only identifies an output error from the previous attempt. It
does not change the evidence or the assessment task.

# Evidence authority

| Decision | Compare | Do not use |
| --- | --- | --- |
| Relevance | Candidate meaning with Question and Gold | retrieval or current SQL success |
| Usage | current SQL with the Candidate-owned contract | whole-query correctness or quality |
| Quality | Candidate with Question, Gold, metadata, and the canonical asset contract | current SQL or retrieval failure |

Judge only the Candidate-local contract. An unrelated SQL error, a wrong final result, or a
different output projection does not by itself make this Candidate unused or invalid.

Condition matching is semantic inclusion, not whole-Question equality. If the Question expresses
the condition inside a larger request, the condition holds. Other independent requirements do not
make a minimal condition too narrow or invalid.

# Decision procedure

Assess every unit in the following order. Do not infer one result from another.

## 1. Relevance

Ask whether Question names the Candidate's complete meaning or behavior and Gold implements it.
Name overlap or use of one shared physical operand is not support for a different definition.

- `supported`: both Question and Gold support it.
- `unsupported`: it is absent from or contradicted by Question or Gold.
- `unknown`: the supplied evidence cannot decide.

Every Metric root must answer the Question. A dependency need not be requested separately, but it
must be necessary to calculate a root.

## 2. Usage

1. If `retrieval_status=not_retrieved` or `not_selected`, return `not_used`.
2. If selected, compare `current_sql` only with the Candidate-owned contract.

Apply the contract by type:

- Concept: SQL uses the target as the complete implementation of this Candidate's Question term,
  with equivalent Columns, values, predicates, and boolean structure. A target used only as an
  operand inside another Candidate's formula or operation is not application of the Concept. A
  value-preserving selector over a directly stored term remains external to the Concept contract:
  using the correct target under that selector is still Concept usage.
- Metric set: `used` means `current_sql` contains the Metric formula, dependencies, and owned plan
  steps. External filters and groups do not become Metric content merely because they select records
  before the formula. A conditional branch in the Candidate expression is Metric content only when
  the Metric term explains it as part of the term's one-to-one implementation. Do not compare the
  whole current and Gold queries; return `not_used` only
  when the Metric formula or an owned step is absent or changed.
- Generation Rule: when the condition holds, compare only the rows, values, and operations declared
  by the action. Outputting an entity ID, name, or full row is irrelevant unless the action owns that
  choice. SQL operation names may state the required behavior directly.

Return `used` for complete application, `not_used` for absent, partial, or changed application, and
`unknown` only when `current_sql` cannot decide.

## 3. Intrinsic quality

Ignore `current_sql` and `retrieval_status` in this step.

Use this order:

1. Split the Question into the smallest terms or request operations that can each be matched to
   their own complete implementation.
2. Check that the Candidate represents exactly one such unit and that its content matches that unit
   in both directions. Independently useful formula operands do not prove that the Question term
   itself can be split.
3. Check that the canonical owner matches that unit.
4. For a Metric, check that name, description, pattern, and expression describe that same unit and
   only that unit. The expression is the complete calculation formula for the term.
5. Keep only the references and dependencies required by that formula.

Then check the implementation:

- Concept: trace the complete Gold expression for the Question term. Each target must directly
  return or test the final stored meaning. If Gold must aggregate or calculate from that target to
  produce the term, the target is only an operand and the Concept is invalid. Similar Question,
  Column, or metadata wording does not change this test. Before accepting `all_of`, align each
  independently meaningful Question qualifier with its smallest complete Gold binding. The
  Candidate is not atomic when a proper subset implements such a qualifier; predicates stay
  together only when none is independently meaningful. A continuous compound modifier does not
  override this test; its words may ground separate names with a shared Question head entity. A
  qualifier implemented only by a sibling target must not remain in this Candidate's name.
- Metric: first separate a standard aggregate request only when the Question itself applies a
  replaceable operation to a complete Concept, entity set, stored value, or existing Metric. Replace
  the operand: if the remaining wording is the same generic instruction, the aggregation is
  Rule-owned. Do not infer that split merely because Gold contains an aggregate: when Gold only
  constructs the complete named business quantity by aggregating finer-grained raw values, the raw
  value is an operand and the quantity may be Metric-owned. Otherwise, one indivisible business
  quantity term must intrinsically require its formula. Distinguish external row selection from a
  conditional branch that constructs the formula's operands. A condition may remain in the Metric
  when Gold proves it is part of the Candidate expression and removing it changes the named
  quantity's result; the Metric name does not have to repeat the discriminator or formula constant.
  Then check the term and expression in both directions. The expression contains the complete
  formula and only that formula. It keeps everything the formula needs and excludes behavior
  separately requested by the Question. Preserve intrinsic calculation constraints even when the
  Question leaves their implementation implicit.
  A separate Concept for a population qualifier does not make a same-qualified Metric complete
  without the predicate that restricts what the Metric counts or measures.
  Description, pattern, and expression must agree with Gold. References contain exactly the inputs
  used by the expression, and every necessary dependency is present.
  For the generation plan, check only whether it copies a concrete value supplied solely for this
  request. A formula constant is not a request value. Do not reject a value-free request role or an
  incomplete explanation of the formula.
- Generation Rule: one minimal Question-only condition causes one complete action. Several SQL
  steps may stay together only when all are necessary for that one behavior. Condition, action,
  reason, intent, triggers, and references that the condition or action cannot work without agree. Reject current literals,
  aliases, a full query plan, a Concept binding, a Metric formula, or another independently
  triggered behavior. Do not reject a Rule merely because it names SQL operations or because its
  condition is shorter than the full Question.
  A standard aggregate over an already complete operand belongs here; the operand itself keeps its
  Concept or Metric owner. Reject a Rule that repeats the formula of a business quantity that
  independently passes the Metric identity test.

### Concept–Metric definition choice

When a Concept and a Metric have the same name, treat them as competing definitions of one atomic
Question term. At most one may be `supported + used + valid`.

1. Fix the smallest atomic Question term before reading either Candidate definition. Trace that term
   once to its complete Gold implementation; names, shared Columns, metadata labels, or overlapping
   descriptions do not decide the owner. A Candidate description cannot add a missing qualifier
   such as per-record or per-row to make a raw target complete.
2. Test the Concept first: would its target alone return or test that exact Question term at the
   term's semantic grain? If yes, the stored meaning is complete. If Gold still must transform raw
   row values into the named quantity, the target is only an operand and the Concept definition is
   less accurate.
3. Before calling that transformation a generic request operation, require the Question itself to
   split into a separately expressed replaceable operation and a complete operand term. Do not infer
   the operand term from a same-labeled raw Column or from the Concept Candidate. If the atomic term
   cannot be split this way and Gold needs the declared calculation to produce it, the Metric is the
   accurate definition. Mark the operand-only same-name Concept `invalid` with an `ownership`
   defect.
4. Conversely, a value-preserving `MIN` or `MAX` used only to choose one existing stored value from
   a group does not create a calculated quantity. The correct target is still used; keep the Concept
   valid and mark a same-name Metric with an `ownership` defect. Do not call the direct binding
   unused merely because SQL wraps it in that selector.
5. After this comparison, at most one conflicting Candidate may be `supported + used + valid`. If
   evidence cannot establish either definition, use `unknown` or `inconclusive`; never accept both.
   Continue to report the three statuses separately.

For a Generation Rule, verify cross-field consistency explicitly: `action` and `reason` must assign
the same observable behavior to the stated SQL operation, and the contrasted alternative must not
be described as producing that same behavior. A contradiction between these fields makes the
Candidate invalid even when the action names an operation used by Gold.

Example: a Question asks for the earliest dispatched shipment number. A Rule says to order
dispatched shipments by dispatch date ascending and return one row. SQL that performs that ordering
and limit uses the Rule even when it selects only `shipment_number`, not the full shipment row. The
Rule is not invalid merely because the action names ordering and limiting.

Metric boundary contrast: a current-report `WHERE` filter does not become part of a Metric merely
because it precedes the formula. Preserve a conditional branch only when the Metric term explains
it as part of the term's one-to-one implementation; SQL position and numerical effect do not decide
the boundary.

For a Metric set, every root and dependency must be valid and every dependency must be necessary.
A rejected or missing Candidate does not transfer its meaning to another owner.

Assessment rows follow units, not assets. A connected Metric set is one unit and produces one row
whose `candidate_id` is the unit's exact `candidate_id`. Inspect every Metric member, but do not add
rows for root or dependency asset IDs. If one member is invalid, keep the one set-level row and name
that member only in `quality_defects.asset_id`.

Return:

- `valid`: the whole Candidate composition satisfies its asset contract.
- `invalid`: a concrete Candidate-local defect is proved.
- `inconclusive`: the supplied evidence cannot decide.

Every invalid result must identify the exact member and field in `quality_defects`.

# Cross-field rules

- `not_used + valid` and `used + invalid` are legal combinations; neither is accepted later.
- Whole-query failure alone changes neither usage nor quality.
- `invalid` requires at least one Candidate-local `quality_defect`.
- `valid` and `inconclusive` require an empty `quality_defects` list.
- `unknown` or `inconclusive` means evidence is missing, not that another SQL behavior is wrong.

# Output schema

Return JSON matching:

{template_output}

- Return one row for every `assessment_units` entry and no other row.
- Copy every `candidate_id` unchanged.
- Give concise, Candidate-local evidence for each status.
- Every defect names a member of the same Candidate composition and one schema field.
- Example: an input unit `candidate_id="revenue_rate_set"` containing `metric.revenue` and
  `metric.revenue_rate` produces one row with `candidate_id="revenue_rate_set"`. It does not produce
  rows for either Metric ID; a member defect goes in that row's `quality_defects`.
- Do not output repair instructions, acceptance decisions, ownership changes, or new assets.
