# Shared Slot and Candidate contract

This contract applies to every Organizer stage.

## Conflict

A Conflict exists only when two or more grounded implementations compete for the same local
semantic role or SQL position in the Question. Hold all other behavior fixed: the implementations
cannot all be used for that role, and replacing one with another can change the SQL behavior or
result.

A difference is not a Conflict when the implementations are equivalent, complementary, jointly
required, apply to separate conditions, or serve different roles or SQL positions. In particular,
expressions or output fields explicitly requested together are separate obligations, not competing
Candidates. For example, a requested maximum and minimum may both appear in the correct query and
must not become alternatives in one Slot.

Before creating a Slot, apply this replacement check: identify the one role or SQL position, then
verify that choosing one Candidate for it excludes the other Candidate from that same role or
position. If both can remain in the correct query at different positions, do not create a Conflict.

## Slot

A Slot is one independent, local, result-changing implementation decision in the Question. Hold all
other decisions fixed: changing this decision may still change SQL behavior or the result.

A valid Slot:

1. asks one concrete decision that a user can answer without answering the rest of the Question;
2. concerns one semantic role or one SQL implementation position;
3. sets `scope` to the one main SQL location or behavior area controlled by the Slot;
4. uses a neutral question that does not assume one Candidate is correct;
5. has at least two grounded Candidates whose statements can each be accepted or rejected as a
   whole;
6. contains Candidates that differ only in the decision named by the Slot; and
7. is separate from another difference when the user can answer the two differences independently.

Before creating a new Slot, compare its controlled decision with every Slot already available to
the stage and every Slot already proposed in the current response. Compare the semantic role and SQL
position, not Slot IDs, wording, or evidence sources. If that decision already exists, do not create
another Slot: add provenance to its matching Candidate or add a grounded Candidate to that Slot.

Do not ask for the answer to the original Question or for a complete query. Several SQL expressions may stay in one Slot only when they form one coupled decision and splitting them would make every resulting option incomplete. Do not use this exception to include unrelated query behavior.

## Common Slot types

Every Slot must use exactly one `scope` value. The scope is coarse: it tells downstream agents where
the local choice applies. It does not replace the Slot question or Candidate statements.

- `output`: final returned content, including SELECT expressions, output column order, extra or
  omitted output fields, and whether an aggregate is returned.
- `filter`: row restrictions, including WHERE or HAVING predicates, literal values, comparison
  operators, ranges, and NULL policy.
- `join`: complete table connection path, join tables, join predicates, and join direction or route.
- `calculation`: local formulas and expressions, including COUNT, SUM, AVG, MIN, MAX, CASE,
  operands, DISTINCT policy, scale, numerator, denominator, and aggregate expression choice.
- `grouping`: GROUP BY grain and what one output row represents.
- `ordering`: ORDER BY expression, direction, and ordering expression scope.
- `row_selection`: LIMIT, top-k, minimum/maximum row selection, and tie behavior.
- `set_logic`: INTERSECT, EXCEPT, UNION, and Boolean AND/OR composition that changes meaning.
- `binding`: which supplied Column, Value, table, entity, or target implements one Question mention.
- `other`: only when none of the above fits; the Slot question must make the controlled location
  unambiguous.

The following types are a checklist of possible local conflicts. They must not suppress another valid
result-changing conflict. A Question may have zero, one, or several Slots with the same scope. Create
separate Slots whenever the user can answer the local decisions independently.

- **Semantic binding:** which supplied `table.column`, Value, or local predicate implements one
  business term or mention in the Question.
- **Predicate or Value interpretation:** which grounded field, literal, operator, boundary, date
  range, matching behavior, or NULL condition implements one requested restriction.
- **Implicit or optional constraint:** whether to apply one grounded local restriction that the
  Question leaves unresolved. Both applying and omitting it require evidence; Memory silence does
  not support an omission Candidate.
- **Calculation:** which complete local formula, aggregate, operands, numerator, denominator,
  scale, DISTINCT policy, or NULL behavior implements one requested quantity.
- **Population, grain, or deduplication:** which entities or rows enter a calculation, what one
  result row represents, or how duplicate records are treated.
- **Operation or selection behavior:** which grouping, ordering, ranking, limiting, tie handling,
  extreme-value selection, or operation order implements one requested behavior.
- **Requested projection or result shape:** which field, entity, detail, aggregate, cardinality, or
  other output form implements content explicitly requested by the Question.
- **Additional output fields:** whether to return only the explicitly requested content or also
  include grounded related fields or details that one supplied implementation treats as part of a
  more complete result. Do not assume more output is better. Do not invent an enrichment Candidate
  from an available or retrieved Column alone. If an additional field independently changes grain,
  cardinality, or another decision, represent that difference in a separate Slot.

These types may overlap in one implementation. They belong in one Slot only when they form one
coupled decision; otherwise keep the independently answerable differences separate. JOIN decisions follow this same contract.

## Candidate

A Candidate is one self-contained statement for its Slot, not a solution to the original Question.
Its full content must be acceptable or rejectable as a whole; a Candidate must not mix a correct
local behavior with an incorrect, unsupported, or unrelated assertion.

A valid Candidate:

1. contains only behavior needed to answer its Slot;
2. omits behavior shared by all Candidates;
3. does not include a projection, filter, join, calculation, grouping, ordering, limit, or other
   decision owned by another Slot;
4. has the same semantic role as the other Candidates and can replace them at that local position;
5. supports every claim with its own Graph refs, Initial SQL evidence, or both; and
6. preserves the supplied tables, Columns, Values, operators, operands, scope, grouping, ordering,
   result shape, and other relevant behavior.

Use concise SQL or SQL-like statements when physical SQL is available. Use unambiguous, composable
table and Column references. Prefer real `table.column` names; use aliases only when necessary to
distinguish table instances, and define them consistently in the statement. Do not transfer a
literal, meaning, population, or qualifier from the Question, another Candidate, or unrelated
evidence.

Candidate differences may involve binding, literals, operators, projection or result shape,
operations, grain, deduplication, tie behavior, or different local interpretations of the Question.
The difference itself must be grounded in supplied Memory evidence, supplied Initial SQL evidence,
or both. Do not invent an alternative merely because it is plausible.

A Column can ground a binding Candidate, such as choosing whether one Question mention maps to
`table_a.column_x` or `table_b.column_y`. A Column by itself does not ground stronger behavior such
as a literal value, comparison operator, aggregate, ordering direction, or tie policy unless that
behavior is also present in supplied Memory or Initial SQL evidence.
A Value can support a literal only when the supplied evidence binds that Value to the relevant
Column.

## Grounding and selection

- `graph_refs` must be exact supplied handles for objects that support the Candidate statement.
- When an expression such as `COUNT(*)` has no direct Column ref, cite the supplied rule,
  alternative, or parent signal handle that supports it. Never leave a Candidate ungrounded.
- `initial_sql_evidence` must be a non-empty verbatim fragment of the supplied Initial SQL.
- Evidence location proves provenance, not semantic correctness.
- Merge equivalent implementations and combine their provenance instead of creating duplicates.
- Do not decide or output a selection mode. Runtime presents every Slot as multiple-selection. Every
  selected Candidate must be simultaneously applicable, and the selected statements together must
  completely answer the local decision. Their combined text must retain the relationships needed to
  use them without guessing which expression applies to which role or SQL position.
- The interaction always allows selecting no Candidate and providing a free-text answer.

## Evidence description

For every Candidate, fill `evidence_assessment`:

- `source_types` lists one or more sources: `initial_sql`, `ontology_asset`, or
  `database_schema`.
- `initial_sql` means the Candidate behavior appears in the supplied Initial SQL.
- `ontology_asset` means the behavior comes from a supplied Concept, Metric, Generation Rule, or
  Conflict Alternative learned from historical cases.
- `database_schema` means the behavior comes from a supplied Column, Value, or complete physical JOIN path evidence.
- Assign each Candidate the paper's relevance score a(c) through `applicability`:
  `applicable` means its implementation directly matches the role and conditions required by the
  Question, scoring 1.0. `uncertain` means it provides relevant implementation guidance from a
  related context, scoring 0.5. This is relevance, not confidence in correctness.
- Assess Initial SQL Candidates by the same relevance criteria; appearing in the current attempt
  does not automatically give a Candidate 1.0.
- `explanation` names the evidence and the exact behavior supported; for `uncertain`, explain the
  related context and how its guidance bears on the current choice.

Do not output a Candidate that provides no relevant implementation guidance for the Question. Two or more uncertain
Candidates may still form a Slot. A Candidate may have several sources; supporting the same
Candidate from several sources is not itself a conflict.

The contrast is the exact behavior that differs between Candidates in one Slot. Every Candidate's
own evidence must contain its side of that contrast. Do not create the opposite side from silence,
the Question, another Candidate, or general knowledge.

Before returning, read each Slot question together with each Candidate. Remove behavior that is not
needed to answer that local question. Split the Slot if its differences can be answered independently.


## JOIN choices

`join_candidates` supplies complete paths, their SQL statements, graph refs, and supporting
business definitions. These paths are evidence for discovery, not preselected user requirements.
Their endpoint tables are possible roles; the Question need not connect all retrieved tables.
Inspect Concepts and their alternative bindings as well as the Initial SQL when determining roles.

Treat JOIN as an ordinary `scope="join"` decision in all discovery stages. Compare paths only for
the same semantic endpoint roles. Keep complete ON predicates, direction, JOIN type, and necessary
routing tables together. Preserve a complete path even when some of its components are shared.
A Generation Rule or its alternative supplies semantic evidence only for behavior it actually
prescribes under its stated condition. A physical path supplies `database_schema` evidence.
Cite actual supplied graph refs, never a path-list position or an invented path ID.

Do not invent a JOIN type or ON predicate from model knowledge. A supplied physical edge can support
its declared default JOIN type, but its columns alone cannot justify a different JOIN type. The
Initial SQL may independently supply an alternative, supported by its exact local SQL fragment.

Merge equivalent paths and their provenance. Paths jointly needed to connect different endpoints
are complementary obligations, not alternatives. Separate independently answerable relationships;
there is no global one-JOIN-question limit. Keep a coupled connection plan together only when its
parts cannot be answered independently. If endpoint binding and path choice cannot be answered
independently, retain their complete, evidence-supported alternatives in one choice rather than
assuming an unconfirmed binding or asking a duplicate binding question.
