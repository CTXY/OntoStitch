# Extraction steps

1. Identify minimal reusable business meanings, calculations, occurrences and generation behaviors from the case and relevant data. SQL alone does not establish taxonomy or roles.
2. Reuse matching knowledge after reading its definition. Do not create every type/field/value or a whole-question node.
3. Give each Concept valid nonempty matching groups. Preserve joint conditions. Targets may overlap; direct grounded_on edges must respect endpoint uniqueness. Reuse verified Values.
4. Generate all supported relationships explicitly, with precise predicates and concrete descriptions. State conditions, missing-value limits or prohibited inferences when relevant. Write related nodes/edges atomically; never change formulas to obtain unrelated edges.
5. Create Event only when the event structure is evidenced; create Metric only for an actual business calculation. A named stored quantity belongs to Concept.
6. Use the full operation registry. A Rule needs an evidenced choice between consequential implementations beyond Concept bindings or Metric formulas. State the Gold-supported minimal behavior; ordinary SQL syntax is insufficient. Reuse fitting intents and justify extensions. Linked business names are allowed; parameterize runtime values. Leave SQL-side triggers empty; do not create ordinary join-fact Rules or invent errors.
7. Check defects in definitions, matching groups, formulas, relation meanings and overall business organization. No coverage score or type quota applies. Deliver for validation.
