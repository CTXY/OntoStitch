from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal

ONTOLOGY_ASSET_DEFINITION_VERSION: Final = "database_ontology_assets_v26"

AssetContractSection = Literal[
    "common",
    "owners",
    "concept",
    "metric",
    "generation_rule",
    "training_construction",
]

COMMON_ASSET_RULES: Final = """## Common Asset Rules

An ontology asset is reusable knowledge, not a patch for one Gold SQL.

- Concrete physical bindings are allowed; concrete does not mean query-specific.
- A stable named entity, member, category, period, or event may be a Concept, even when it limits
  the current Metric calculation.
- A value supplied only to instantiate a separate request operation is runtime data, including
  thresholds, date boundaries, interval endpoints, and K. Values required by an asset's own
  definition follow that asset's contract.
- Temporary aliases, full SQL, case IDs, and replay traces are evidence, not asset content.
- Gold proves how Question meaning is implemented; it cannot create unmentioned meaning.

For each current-case detail, choose one treatment:

- Remove current entity, date, member, threshold, K, and other supplied values when the asset
  remains complete without them. Keep a field name only when its role in the proved implementation
  is useful, and state that role clearly.
- In Generation Rule prose, refer naturally to a supplied input, such as “the date specified in the
  Question” or “the requested number of results”. Do not replace business language with role
  placeholders.
- Keep physical binding, formula, counted object, required calculation grain/order, source
  alignment, or required NULL/zero behavior only when the term explains that content.

Do not hide field roles as “specified scope” or “relevant field”. Naming a current field role in an
Initial Metric plan does not make it part of the Metric formula. Do not add a wider scope, default
behavior, or unobserved branch; those
decisions belong to Generalization.

Every persisted reference must use an exact ID supplied by the physical graph, visible accepted
Working Ontology, or a temporary Candidate set that this contract explicitly permits to create.
Co-occurrence in one Question, SQL, CTE, or repair case is not a dependency. Keep a reference only
when removing it would make the asset's own definition, applicability, or required behavior
incomplete.
"""

OWNER_BOUNDARIES: Final = """## Owner Boundaries

First state the smallest complete knowledge needed to fix the error. Then choose one owner from the
meaning of that knowledge, not from the current SQL shape or the assets that happen to exist.

### Concept

- Maps one atomic Question meaning to its stored physical representation.
- The binding is local to that value: a Column, a Column-value predicate, or a small source-local
  representation transform.
- It does not compare or choose among rows, aggregate records, follow a selected row to another
  entity, or coordinate query steps.

### Metric

- Defines one smallest Question term whose one-to-one implementation necessarily includes a
  calculation.
- Split a phrase only when the smaller terms and their respective complete implementations can each
  be identified independently. An independently matched request operation is handled under the
  Generation Rule contract.
- The term and implementation match in both directions: the implementation completely expresses
  that term, and every part of the implementation is explained by that term.
- A raw operand is not a same-name Concept unless the Question also names that stored value
  independently.

### Generation Rule

- Describes how the Question applies meanings and calculations that are already complete.
- It may filter, select, group, order, rank, coordinate entities or calculations, or control other
  reusable SQL-generation behavior.
- It does not redefine a Concept binding or a Metric formula. Directly projecting an explicitly
  requested, already-bound value is SQL Generation behavior and has no ontology owner. A selective
  result policy such as retaining a requested grouping dimension after that dimension is filtered
  to one member may be Rule-owned because it chooses between result shapes under a recognizable
  condition.

Use this order:

1. Write the smallest complete obligation independently of the erroneous SQL.
2. If it ends at a local physical binding, choose Concept.
3. If an indivisible, self-contained quantity term requires an intrinsic formula, choose Metric.
4. If it controls how existing meanings or quantities are used, choose Generation Rule.
5. Split independently reusable obligations. A one-off SQL patch has no ontology owner.

Projection and pure MIN/MAX preserve a stored value; their selection behavior is not a Metric. A
general query aggregation over a complete Concept, entity set, stored value, or existing Metric
also does not create a Metric. Keep the operand's owner and express the requested aggregation as
Generation Rule behavior. A named new quantity remains a Metric only when the formula is part of
that quantity's own business meaning.
"""

CONCEPT_CONTRACT: Final = """## Concept Contract

Concept rules:

- It owns one atomic stored meaning, field, entity, member, qualifier, or category.
- A target directly returns or tests that same meaning at one source location.
- A requested output entity binds to the identifier or value returned for it; a predicate only
  binds its qualifier.
- `references` lists the exact physical Columns and governed Values used by the target.
- `sql_expression` is the executable binding, not a general query filter.
- A target may use a value predicate or small representation transform.
- A Concept does not build a measured quantity with cross-row aggregation or arithmetic.
Column/Value implementation shape alone does not establish Concept ownership. Before writing a
target, verify that it directly returns the same business value at the Concept's intrinsic semantic
grain; current-report grouping and result cardinality do not change that asset-level grain. When a
validated Metric covering the complete atomic phrase intrinsically produces the named quantity and
the proposed Concept Columns are only raw operands of that value-constructing formula, the Metric is
the sole owner unless the operand is independently named outside that phrase. Thus “总订单量” does
not also create `concept.订单量`; “订单量及总订单量” may support both. Identity projection and pure
MIN/MAX selection of one stored value
are value-preserving and do not suppress the stored-value Concept.
The absence of a Metric Candidate does not establish Concept ownership. Initial extraction may be
incomplete; when Gold constructs a named value by aggregating or calculating raw operands, do not
publish an operand-only Concept even if no Metric proposal covers that mention.
Distinct concepts remain distinct
even when SQL combines them with IN or OR. A database-trained mapping needs an executable target:
a stable target ID, SQL expression, complete typed physical references, and an explicit endpoint
combination operator when needed. It does not own aggregation, metric formulas, projection,
grouping, ordering, ranking, limits, or query-branch coordination. Each target must itself carry the
same business meaning; a column used only as an operand does not map the computed number produced
from it. Interpret a mention in its complete Question context; substring or column-label overlap
does not establish an independent Concept. The same Question meaning cannot be owned by both a
Concept and a Metric: when a Metric computes that named value from a Concept target, the same-name
Concept is invalid. Extract and Optimizer outputs do not generate aliases.

Classify values by their Question role, not by literal type:

- A stable named entity, member, category, status, period, or event may use a complete Column–Value
  target. Replacing the name creates another Concept.
- A value used only as a threshold, boundary, interval endpoint, K, or other operation input is
  runtime data and does not create a value Concept.
- A normal report year, month, or quarter is runtime scope. Only a governed named fiscal period,
  event, campaign, or cohort may be a period Concept.
- A request condition outside a Metric term may still contain permanent Concept knowledge.
- A field and one of its members are different Concepts when both are explicit and useful. Bind the
  field to its Column and the member to its complete Column–Value predicate.
- A member name cannot map only to the broader Column. A field name cannot map to one member.
- Do not invent a field from Schema alone or add a Rule that only repeats a member binding.

MIN/MAX in Gold SQL does not by itself make the selected Column a calculated measure. When the
Question mention names the stored value rather than “highest” or “lowest”, keep the Concept and put
the selector in a Generation Rule. A wrong selector in current SQL is Candidate usage failure, not
an intrinsic Concept defect.

A canonical target writes only id, sql_expression, references, and binding_operator. When a target
references Dimension Values, those values are its semantic endpoints and owner Columns are physical
support only. Otherwise its Column references are the semantic endpoints. The binding operator
applies only to semantic endpoints.

Write physical bindings literally and completely. Every Column token in `sql_expression` uses the
catalog `table.column`, never an unqualified name, query alias, CTE alias, ontology ID, or synthetic
placeholder. `references` is the exact typed closure of that expression: include every used Column
and every governed equality/IN Dimension Value, with no missing or extra references. A field,
entity, or dimension meaning uses its directly grounding qualified Column; a governed value or
predicate uses the complete qualified predicate. Do not invent `IS NOT NULL`, `1 = 1`, or another
predicate merely to make a Concept executable.

Use one of these implementation shapes. A Column implementation writes the qualified Column itself
and references that Column. A governed-value implementation writes the qualified value predicate
and references its owner Column plus the matching Dimension Value IDs. A representation
implementation writes only the minimal source-local transform that produces the named field/value
and references its input Columns. `IS NULL` and `IS NOT NULL` express row-existence selection, not a
Column or governed-value implementation, and cannot be used as Concept targets.

Universal determiners such as “all”, “every”, “全部”, “所有”, “各”, and “每个” usually state that
the current query applies no additional restriction over a population. Absence of a filter is
runtime scope, not a reusable physical predicate. Extract the independently named base Concept only
when Gold SQL directly realizes it through a physical Column or predicate; otherwise emit no
Mapping for the universal phrase. Never turn universal scope into `column IS NOT NULL` or a constant
true expression.

Schema availability is not realization evidence. Every target Column must participate in Gold SQL
lineage that directly implements the named mention; a plausible catalog Column unused by Gold SQL
cannot ground a Candidate. Adverbial query-grain phrases such as “按月”, “逐月”, or “按年” instruct
the current query to group or present results at a grain and are handled under the Generation Rule
contract.
They are not Concepts. A noun-like independently named time field or value such as “月份” may remain
a Concept when Gold directly realizes that meaning through a physical Column or representation
transform.

Determine `binding_operator` from semantic endpoints after separating supporting Columns. Use
`exact` for exactly one semantic endpoint, `all_of` when all endpoints jointly realize the one
atomic meaning, and `one_of` when alternative endpoints inside the same source-local expression
independently realize it. AND predicates normally imply `all_of`; OR/IN alternatives normally imply
`one_of`. Different source-local realizations remain separate targets rather than endpoints of one
target. Multiple targets are legal only when the same one NL meaning is independently realized in
multiple Gold source locations and every target completely implements that meaning by itself.
Jointly required SQL endpoints stay together in one target. Independently meaningful NL mentions
remain separate Concept Candidates even when they share one physical target.

Before using `all_of`, align the smallest independently meaningful Question qualifiers with the
smallest complete Gold bindings. Emit separate Concepts when each qualifier has its own complete
binding, even when the query applies the predicates together. Use `all_of` only when no proper
subset of the endpoints implements an independently named meaning. Predicate count alone does not
decide the boundary: several Columns may jointly encode one indivisible stored meaning. A
continuous compound modifier may ground several Concepts; each name selects only its
binding-specific Question words, adds the shared head entity, and uses only necessary grammar
changes. A modifier implemented only by a sibling target must not remain in the current Concept's
name; shared head entities may repeat.
"""

METRIC_CONTRACT: Final = """## Metric Contract

Metric rules:

- It defines the smallest Question term that needs one complete calculation.
- Split out a smaller term when it separately matches an entity, stored meaning, existing Metric,
  or requested SQL operation. Keep the complete term as well only when it needs its own calculation.
- Use a split test before creating a Metric: if the proposed Metric name can be split into smaller
  parts and each part has its own matching SQL fragment, the whole phrase is not one atomic Metric.
- If the phrase only asks to apply an operation to a complete entity, stored value, or existing
  Metric, do not create a Metric for the combined phrase. Keep the complete phrase when it names a
  calculation of its own. Decide from the Question meaning, not the SQL function used by Gold.
- `number of X` or `count of X` is not a Metric when X is already a complete Concept, entity set,
  stored value, or existing Metric and Gold implements the remaining words as `COUNT(X)`.
- The expression is the calculation formula for this Metric only. Do not copy other Question
  concepts, filters, grouping, ranking, ordering, or display instructions into it.
- The expression contains every operation and condition required by the formula. Its refs contain
  only the Metrics and Columns actually used by that formula; fixed formula values remain in expr.
- Preserve every Gold function, cast, date conversion, operator, and operation order used by the
  Metric formula. Plan prose cannot replace any part of the expression.
- Description, pattern, and expression describe this same quantity. Do not independently
  summarize the whole Question again when writing each field. Description and pattern explain the
  Metric formula only; they never repeat a filter or grouping that the Question separately requests
  from that calculation. Keep conditions required by the formula even when Question or evidence
  explains them outside the Metric name.
- A request operation that independently matches its own implementation is handled under the
  Generation Rule contract rather than included in another term's Metric.
- Once Metric identity is established, preserve its defining population and conditional formula,
  even when the Question does not spell out their implementation. A plan cannot be their only owner.
- The plan implements exactly the expression; every expression operation appears in the plan.
  Calculation steps must implement that expression. Request field roles may explain how its
  input records are selected or grouped without adding formula semantics.
- The plan owns the physical source, counted object, input grain, and cardinality required to
  implement that exact formula; these are not separate Rule behavior.
- The plan may contain physical bindings and variable formula roles, but not current output shape,
  sorting, limit, temporary aliases, or full SQL.

A stored value is a Concept, not a Metric. A similarly named stored Column may still be only an
input when Gold must calculate the complete Question quantity from it. Replace that operand while
testing a standard aggregate request; do not classify from the SQL aggregate alone. Keep the
measured subject and do not use names such as “rate”, “share”, or “growth” by themselves. A pronoun,
vague phrase, raw field label, or helper alias cannot be rewritten into a Metric identity.

Apply Question grounding and minimization in this order:

1. Start from the smallest exact Question phrase that still names the complete calculated quantity.
2. Remove entity, member, ordinary time, threshold, grouping, ranking, ordering, and display words
   when the same quantity and formula remain without them.
3. Keep a fixed population, formula role, or required grain only when removing it changes the
   quantity itself rather than a separately matched request condition or operation. A selected
   entity or member does not become a fixed population merely because the Question names it or
   Gold filters it. Keep separately matched selections out of description, pattern, and expression;
   their field roles may appear in the plan.
4. Trace the complete Gold expression for the retained term. Keep every value-changing operation,
   and reject defining calculation behavior that is not required by that term. A plan may also
   explain the separately requested field roles allowed below.

Do not drop words needed to name the measured quantity; a generic formula word cannot name a
quantity-specific Metric. Preserve that identity during repair. Current/prior,
numerator/denominator, and other formula roles do not create Metric identities; reuse one Metric ID
when the measure definition is unchanged. Unit and scaling
must be supported by the Question and Gold formula together; a descriptive label alone cannot
override formula evidence. Aggregation or formulas such as SUM, COUNT, AVG, ratios, and arithmetic
belong here only when they define this exact Metric mention; their operands must not be duplicated
as Concepts named after the computed result. Extract and Optimizer outputs do not generate aliases.

Generation plan content and scope:

- Allowed: current/prior roles, numerator/denominator roles, internal grain, and cross-source
  alignment when the formula needs them.
- Write source-to-result steps connecting actual physical fields to their business meaning and
  how each operation distinguishes or combines records. Use the supplied Schema and evidence for
  business meanings, not guesses from identifiers. Explain what constitutes one counted instance,
  which records contribute to an aggregate, or which results correspond, as the operation requires.
  Preserve the actual operation, including its counted object and NULL behavior. Explain
  input selection and grouping where they act, before the aggregation they affect, not as notes
  appended after the calculation. The plan must be understandable without the original Question.
- Filtering and grouping separately requested by the Question may be explained as input uses.
  Name the real field and say only that it selects inputs or organizes results; do not state a
  comparison, equality, range, membership test, value, or predicate, and do not require that use
  for every calculation.
- It is valid to omit a request field role. If including it would repeat or imply the request's
  predicate, value, or mandatory grouping, omit it instead.
- Omit the specific values and concrete selection predicates explicitly supplied by the Question
  solely for this request. Keep the fields' business roles, not instantiated entity, member, date,
  threshold, or K conditions. Preserve formula-owned predicates, constants, population, internal
  grain, and order even when the Question leaves their implementation implicit. Explicit or
  implicit wording alone does not decide whether a condition belongs to the formula.
- Keep the proved physical implementation; do not widen fields into arbitrary dimensions or add
  unobserved defaults or paths. Do not use `{question_filters}`, `{grouping_fields}`, or other prose
  placeholders. Broader applicability requires later Generalization evidence.
- For multiple sources, explain their respective inputs and operations, then the evidenced
  correspondence between their results. Do not infer identical filters or records across sources.

Initial Metric output contains only `name`, `description`, `pattern`, `expression`, and one
`sql_generation_plan`. Code derives the stable asset ID, empty Initial aliases, and direct
`references` from `expression.refs` only. Physical plan tokens remain local to their plan and do
not become Metric references. An emitted Metric dependency uses
`metric.<exact asset name>`, never its plan ID or SQL alias. Plan sources use `<table>` and fully
qualified `<table.column>` tokens; bare Column tokens are not part of the format.

A new Metric needs naming evidence: an exact atomic Question mention or, only for a supporting
dependency, a trustworthy business-facing Gold output alias. Never create a Metric from a formula
component, raw column label, helper/CTE alias, or an inferred intermediate name. Keep an unnamed
intermediate calculation inside the enclosing Metric expression.

A Column reference denotes that Column's stored value, not an unstated calculation over it.
Reference a derived value through a Metric only when that Metric has a valid independent identity;
otherwise inline the complete derivation in the enclosing expression. Placeholder names and plan
prose cannot turn a raw Column binding into a calculated value.

Contrast: if a Question names only `delivery completion rate` and Gold divides delivered shipments
by eligible shipments, emit the rate and keep the unnamed numerator and denominator calculations
inside its expression. Emit a dependency only when the Question independently names that quantity
or a trustworthy final business output alias names it. Internal CTE names and helper aliases do not
supply Metric identity.

Precision operations such as `ROUND(..., N)` belong to the Metric only when Question intent and Gold
together establish an intrinsic business precision or rounding policy. A final-SELECT ROUND used
only to format the current report is Generation Rule presentation and must not enter the Metric
expression, pattern, or generation plan.

A Metric may use a directly stored Concept value or predicate as an operand. A separate Concept for
a fixed population qualifier does not remove that predicate from the same-qualified Metric formula.
If Gold SQL applies
MIN/MAX to that operand only to select a value for the current query, the selector is not a missing
Metric formula or plan clause. Wrong grouping or selection is Candidate usage failure unless the
Metric itself prescribes it.

expression.refs binds formula placeholders. properties.references is exactly the canonical
dependency closure used by expression.refs. A metric.* dependency appears in both. Metric
does not write filters, allowed_dimension_refs, or Metric-local default_time; intrinsic calculation
and filter truth must be explicit in expression and references. Metric references compile to
depends_on; Table, Column, and Dimension Value expression references compile to grounded_on.
Plan-only physical tokens remain implementation guidance and do not compile dependency edges.
Graph edges provide dependency reachability and do not duplicate formula, filter, dimension, time,
or plan roles.

Keep the two physical-reference formats separate:

- `expression.refs`: `column.orders.amount` or `metric.<exact asset name>`.
- Pattern/plan Table token: `<orders>`.
- Pattern/plan Column token: `<orders.amount>`.

Every Column occurrence in Pattern or plan prose must be self-contained and fully qualified. A
nearby `<table>` does not qualify a later `<column>` token. In a multi-source plan, do not write
“aggregate `<orders>` by `<department_id>`, aggregate `<targets>` by `<department_id>`, then match
by `<department_id>`.” Write each source-specific occurrence, for example aggregate by
`<orders.department_id>` and `<targets.department_id>`, then match
`<orders.department_id>` to `<targets.department_id>`. Never put `<column.orders.amount>` in plan
prose or `<orders.amount>` in expression refs.

Initial candidate sets follow formula dependencies, not output layout. Put each independent
Question-facing Metric root in its own set. Combine roots only when they share a newly emitted
Metric dependency; shared filters, grouping, source tables, final projection, or result coordination
do not connect otherwise independent roots.
"""

GENERATION_RULE_CONTRACT: Final = """## Generation Rule Contract

Generation Rule rules:

- It owns one causally connected, result-changing SQL-generation behavior.
- The behavior applies or coordinates existing knowledge. It may control reusable result behavior,
  but it does not merely choose final output Columns.
- It does not define a Concept or calculate a named Metric.
- The asset type is open-ended. Each extraction stage may limit which behaviors it can propose.
- It needs direct Question and Gold evidence. It is not a patch for one SQL query.
- `operation_intent` is one optional direct field on the Rule. Initial Extraction records its
  Registry intent. Repair may record one primary behavior kind. It does not replace
  condition/action, define identity, or prove correctness.

Condition and action:

- `condition` is the smallest runtime-visible situation that makes the whole action necessary.
  It is the final applicability test, not a bag of retrieval terms. An Optimizer may safely
  generalize it only when the broader wording still handles the same kind of business data, uses
  the same action, distinguishes the same incorrect alternative, and excludes cases that require a
  different algorithm or boundary.
- The condition contains only meaning observable before the Rule's decision. It must not state the
  required result, the chosen SQL representation, the postcondition, or the contrast conclusion.
  Those belong to `action` and `reason`. For example, a request for a quarter dimension may trigger
  a Rule that supplies a year-qualified quarter key; the condition must not presuppose that the
  Question already requested a year-qualified key.
- In Initial Extraction, every condition fact must be supported by the Question trigger.
  A Table, Column, JOIN, subquery, key, NULLability, duplicate-row risk, or Schema cardinality
  inferred from Gold/Schema cannot appear in the condition.
- `action` states one complete behavior that SQL generation can carry out. It may combine concise
  business prose with a local parameterized SQL pattern when the pattern makes the decision easier
  to execute and verify. A pattern may use role-labelled placeholders such as `<date_column>`,
  `<group_dimension>`, or `<requested_k>` inside the SQL fragment. It must define each role in the
  surrounding prose and must not become a full query, retain a temporary alias, or copy a current
  literal. Stable physical Table/Column bindings may be named directly.
- The condition must cause the whole action. Several SQL operations may stay together when each is
  needed for the same behavior. Repair Review separates independently evidenced errors before a
  Rule is optimized; later stages must not reopen that frozen boundary.
- When a failed replay uses a proved competing behavior at the same decision point, the Optimizer
  must state both `Required behavior` and `Avoid behavior` in `action`. Contrast the source and
  immediate result of the two behaviors. When the action was only omitted, state only the required
  behavior. Keep the contrast reusable: describe the competing behavior type without current
  literals, aliases, ontology IDs, or unrelated query structure. An Avoid description that also
  matches the required Gold behavior does not distinguish the two behaviors and must be rewritten.
- Required and Avoid behavior must operate on the same business data, produce the same kind of
  immediate result, and differ in only one behavior. If they change two independently reusable
  behaviors, the Rule boundary is invalid.
- Do not duplicate a correction merely because the same business data and behavior appear in
  SELECT, GROUP BY, JOIN, different aliases, or CTEs. These repeated SQL locations are evidence for
  one correction.
- Similar behavior on different business inputs may be described together when the current
  evidence clearly supports one shared action. It may instead remain separate when each input can
  be described, repaired, and validated on its own. Do not force either result from the number of
  Tables, Columns, sources, CTEs, or SQL clauses. Graph Merge later decides whether separately
  accepted Rules have the same identity after source-specific names are replaced by their roles.
- Use only the Question business words needed by condition and action prose. Do not replace those
  words with abstract prose placeholders. Role-labelled placeholders are allowed only inside a
  local SQL pattern as described above.
- Do not copy current dates, thresholds, K, entity values, or member values into the permanent
  Rule. Refer to them naturally, for example “the date specified in the Question” or “the requested
  number of results”.
- Include a reason that compares the action with another plausible implementation of the same
  requested operation and states the immediate observable difference. Omitting the operation is
  not an alternative implementation; for an evidenced omission, state only Required behavior and
  explain the missing direct postcondition without inventing an Avoid implementation.
- The Rule must be understandable from its own fields, resolved references, and relevant Schema
  bindings. A reader must be able to tell when to use it, what business data it handles, what the
  SQL must do, what result that step produces, and which evidenced alternative it rejects. If this
  meaning can be understood
  only by reopening the source Question, Gold SQL, erroneous SQL, CTE layout, or evaluation record,
  it is not a valid permanent Rule.

For repaired Rules, describe one operation point as `complete business input -> one behavior ->
first complete result`. Use these plain behavior kinds to find its boundary:

- `relationship_path`: connect records through the required business relationship; output the
  related records.
- `row_constraint`: keep or exclude rows from an existing record set; output the filtered rows.
- `grouping_grain`: decide which rows produce each calculation; output per-group inputs or results.
- `result_selection`: choose rows or values from an existing candidate set; output the selected
  results.
- `calculation_composition`: combine complete operands under a Question instruction; output the
  derived value. A formula owned by a named Metric is not this kind.
- `result_coordination`: align or combine already-complete result sets; output the coordinated
  result.

These kinds are an open review checklist, not a closed Registry. Repair may store one primary kind
in `operation_intent`. The kind labels the main knowledge obligation; it does not by itself split
SQL symptoms into errors. Split only when either correction can remain coherent and useful while
the other difference is left unchanged. Keep several SQL changes together when they are the
positive and negative sides of one decision, or none is a complete correction alone. A possible
CTE rewrite alone does not prove a boundary. When an evidenced relationship or constraint
difference ends in a complete candidate set, a separately wrong selection, grouping, or
calculation over that set is another error. Do not split one correction merely because its SQL
pattern is repeated for the same business input in several SQL locations.
Repair Review freezes this boundary; Optimizer and Assessment do not split it again.
`operation_intent` cannot override owner boundaries.

Initial Extraction has no predicted SQL:

- Initial output must use exactly one supplied Registry value in the direct `operation_intent`
  field. Do not add a metadata object or replace `condition` with an Intent name.
- The condition and Question triggers must use only NL meaning visible in the Question.
- SQL-side Column and operation triggers must be empty.
- Gold SQL proves the action. Its SQL operations and necessary physical bindings may be named in
  the action, but must not appear in the condition or retrieval triggers.

Boundaries:

- A stable Question-named entity, member, category, or predicate is Concept-owned. Do not create a
  Rule that only repeats its equality binding.
- Selection and presentation such as min/max, ranking, top-N, and best/worst may be Rule-owned when
  they are reusable. They do not turn a stored Concept value into a Metric.
- Direct projection of an explicitly requested available binding is not Rule-owned. A missing
  term-to-value binding belongs to Concept, and simple failure to select that available value
  belongs to SQL Generation. A selective dimension-retention, pivot, label-preservation, or result-
  shape policy may be Rule-owned only when its runtime condition and competing result behavior are
  independently reusable.
- `triggers.question.phrases`, `triggers.sql.columns`, and `triggers.sql.operations` are independent
  OR retrieval signals. A signal means that the Rule is likely relevant when it appears; it does
  not prove the condition. Multiple matching signals may increase recall confidence.
- Question phrases may safely cover several NL forms of the same decision family. SQL Columns are
  canonical Column IDs whose use is strongly associated with this Rule; SQL operations are
  behavior clues from an incorrect draft. Do not add weak co-occurrence, unrelated output phrases,
  or unproved neighboring cases such as fiscal-year or period-over-period behavior.
- Reference only visible semantic assets or supplied physical nodes that the condition or action
  cannot work without. All references are required together for the complete
  Rule contract. A replaceable implementation detail, retrieval-only clue, reason example, or
  query-coincident asset is not a reference.
- The same Column may appear in both `triggers.sql.columns` and `references` when it independently
  has both roles: the former provides OR recall, while the latter records an AND dependency and
  compiles Graph grounding.
- Do not create dependencies, repeat a Metric formula, copy evaluation-only wording, or emit a full
  SQL template.
"""

SQL_TRAINING_CONSTRUCTION_RULES: Final = """## SQL Training Construction Rules

Use only exact IDs in the supplied physical catalog and visible accepted Working Ontology. Concept
Candidates cannot create semantic dependencies. A Generation Rule Candidate may reference Concept
or Metric Candidates from the same extracted case; remove those references if the targets are not
retained. A Metric Candidate may include
new missing Metric dependencies in its current temporary set: reuse an exact visible Working Metric
ID when it exists; otherwise create the complete minimal acyclic Metric closure. dependency_assets
contains only Metrics newly created by this Candidate.

An Initial Metric set does not ask the LLM to repeat derived asset IDs. Code derives roots and
dependencies only from exact metric.* references among set members. A Residual Metric set has one
frozen CREATE/UPDATE anchor plus newly created missing Metric dependencies. Candidate member IDs
are frozen after construction; refinement changes
content for the same IDs and never adds, removes, or replaces members. Invalid output is corrected
by the producing Extractor or Optimizer within its bounded retry.

Instructions and output-Schema descriptions are written in English. Asset natural-language content
follows the Question's primary language: use an exact Question span for Concept names unless a
dependent modifier must be completed with its exact Question-explicit bare entity; prefer the
Question's named business phrase for Metric names; and write descriptions, patterns, plan prose,
Rule names, conditions, actions, reasons, and Question triggers in that language. Canonical IDs,
physical references, SQL tokens, and operators keep their required technical format.

Initial Generation Rule discovery is limited to the supplied Initial Operation Registry. A Rule
must match one listed operation, be explicitly requested by the Question, be directly implemented
by Gold, and not be owned by a Concept or Metric. Error-driven Repair and Optimizer stages may learn
other Rule behaviors when the observed error supplies the missing evidence. Initial output records
the matched Registry value in the direct `operation_intent` field; permanent Rules keep that field
optional and do not use a nested metadata object.
"""

_SECTION_TEXT: Final[dict[AssetContractSection, str]] = {
    "common": COMMON_ASSET_RULES,
    "owners": OWNER_BOUNDARIES,
    "concept": CONCEPT_CONTRACT,
    "metric": METRIC_CONTRACT,
    "generation_rule": GENERATION_RULE_CONTRACT,
    "training_construction": SQL_TRAINING_CONSTRUCTION_RULES,
}


def render_ontology_asset_contract(sections: Iterable[AssetContractSection]) -> str:
    selected = tuple(sections)
    if not selected:
        raise ValueError("ontology asset contract requires at least one section")
    if len(selected) != len(set(selected)):
        raise ValueError("ontology asset contract contains duplicate sections")
    rendered = "\n\n".join(_SECTION_TEXT[item].strip() for item in selected)
    return (
        "# Database Ontology Asset Contract\n\n"
        f"Contract version: {ONTOLOGY_ASSET_DEFINITION_VERSION}\n\n"
        "This contract is authoritative for asset identity and ownership. Task-specific "
        "instructions still govern the current stage's evidence, decisions, and output schema.\n\n"
        f"{rendered}\n"
    )


ONTOLOGY_ASSET_DEFINITIONS: Final = render_ontology_asset_contract(
    ("common", "owners", "concept", "metric", "generation_rule")
)


__all__ = [
    "AssetContractSection",
    "COMMON_ASSET_RULES",
    "CONCEPT_CONTRACT",
    "GENERATION_RULE_CONTRACT",
    "METRIC_CONTRACT",
    "ONTOLOGY_ASSET_DEFINITIONS",
    "ONTOLOGY_ASSET_DEFINITION_VERSION",
    "OWNER_BOUNDARIES",
    "SQL_TRAINING_CONSTRUCTION_RULES",
    "render_ontology_asset_contract",
]
