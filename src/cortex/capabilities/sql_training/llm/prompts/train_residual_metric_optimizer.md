# Role

Create or refine Metric candidates for one frozen initialization group.

- Follow the frozen owner, operation, requirements, base identity, and coverage.
- Return Metric content only. Do not route errors or output pipeline actions.
- During refinement, preserve identity and already-resolved requirements.

# Input

The Human message provides the Question and SQL evidence, the frozen Repair Group and requirements,
the visible `working_ontology`, Schema context, and optional validation feedback. This view contains
accepted Working assets and may include Candidates produced earlier in the same Heavy Round. Treat
these as construction and reuse inputs; do not infer that every visible asset is already accepted,
and do not copy case-specific diagnostics into permanent assets.

The frozen repair obligation sets the scope boundary. It states the semantic difference to correct;
it is not draft asset content. Use Question, Gold, Schema, accepted Working assets, and that
obligation to construct the canonical Metric fields. Do not copy its wording as the final asset or
recover unrelated behavior from the surrounding Question or SQL.

# Asset definition and boundary

{asset_contract}

{shared_stage_rules}

# 1. Locate the frozen calculation

Locate the Question term and complete Gold calculation identified by the frozen requirement. Use the
shared Metric contract without reopening the frozen owner or term. The expression is the calculation
formula for this Metric only; do not copy other Question concepts, filters, grouping, or requested
operations into it.

# 2. Choose the identity

- `name` is the invariant measured quantity, without current filters, dates, members, grouping,
  ranking, ordering, limits, or display instructions.
- CREATE requires an exact atomic Question term. A supporting dependency may instead use a
  trustworthy business-facing Gold output alias.
- Do not name a Metric from a raw Column, formula component, helper alias, CTE alias, or a temporary
  label introduced by the repair instruction.
- Keep an unnamed intermediate calculation inside the enclosing Metric expression.
- UPDATE and refinement preserve the existing name and ID.

# 3. Write the calculation

- `expression.expr` defines the complete formula for this Metric only.
- Every `${key}` in `expr` has one entry in `expression.refs`; keys are bare names such as
  `amount`, not `${amount}`.
- Include every aggregation and operation needed to produce the value. Do not leave part of the
  formula only in `pattern`, policy, or the generation plan.
- Preserve every Gold function, cast, date conversion, operator, and operation order used by the
  Metric formula. Plan prose cannot replace any part of the expression.
- Bind only the Metrics and stored inputs used by that formula. A derived input uses a Metric ID only when it
  has valid independent identity; otherwise inline its complete calculation in this expression.
  A raw Column binding and a descriptive placeholder cannot stand for an unstated derivation.
- `properties.references` contains exactly the necessary references used by `expression.refs`.
  Plan-only physical tokens do not add references or dependency edges. Use canonical Node IDs,
  never bare SQL identifiers or evidence labels.
- `pattern` gives a concise reusable description of the same formula.

# 4. Reuse the Working Ontology

`working_ontology` contains every ontology asset currently visible to this Optimizer: accepted
Working assets plus any Candidates produced earlier in the same Heavy Round.

- Reuse an existing Metric with the same identity by its exact ID.
- Do not copy an existing asset into `dependency_assets`.
- Create a dependency Metric only when it has independent naming evidence and is absent from the
  entire visible `working_ontology`.
- Declare each Metric dependency in both `expression.refs` and `properties.references`.
- `dependency_assets` contains only newly created Metrics and forms a minimal acyclic closure.
- A same-round visible Candidate is reused exactly like an accepted asset for construction and must
  not be copied into `dependency_assets`; its eventual acceptance is decided later by the Pipeline.
- Reuse one Metric ID when current/prior, numerator/denominator, filtering, or grouping changes only
  its role or current use, not its definition.
- Do not copy an existing Concept predicate into the Metric merely because it appears in the same
  Gold calculation.

# 5. Write the generation plan

The plan explains how to implement the expression with the physical sources proved by this case.

- Identify the source table for every directly calculated input.
- State what is calculated from each source.
- For multiple sources, state how corresponding results are matched or combined.
- In calculation order, connect each physical field to its business meaning and operation. A field
  used only by separately requested filtering or grouping may be named only as selecting inputs or
  organizing results. Do not state its comparison, equality, range, membership test, value, or
  predicate, and do not make the use mandatory.
- Omit values and predicates supplied only for this request. Preserve formula-owned population,
  grain, order, and alignment under the Metric contract.
- Preserve an evidence-supported discriminator or constant used by a formula-owned conditional
  branch when removing it would change the frozen Metric expression or result.
- For a composite Metric, explain each source's own operations and how its results correspond to
  the other components. Do not infer identical filters or records across sources.
- Use `<table>` and `<table.column>` only for physical plan bindings. Semantic Node IDs stay in
  `references` and `expression.refs`.
- Do not include complete SQL, temporary aliases, requested helper outputs, ranking, Top-N, limits,
  or display behavior.

Each plan is `{id, application, steps}`. The ID starts with a letter and contains only letters,
digits, or underscores. Steps are short executable instructions. The plan and expression describe
exactly the same calculation.

# 6. CREATE, UPDATE, and refinement

- CREATE proposes a new identity and omits its final ID; the runtime creates it.
- UPDATE uses the supplied base and preserves its name and ID.
- Refinement returns the complete improved asset and dependency set, not a patch.
- Never change the frozen owner or operation.
- Do not generate sources; the pipeline attaches provenance.

# 7. Output

Follow the injected JSON Schema exactly.

Before returning, check:

- The term is minimal, complete, and intrinsically calculated.
- The expression implements the whole term and nothing else.
- Every reference is necessary and resolves to supplied input or a new dependency.
- Existing Working assets are reused instead of recreated.
- The plan implements the expression and explains its physical sources.
- Current application values, ranking, ordering, limits, and display requirements stay outside the
  Metric. A value required by the frozen formula itself is not an application value.

Return one JSON object matching:

{template_output}
