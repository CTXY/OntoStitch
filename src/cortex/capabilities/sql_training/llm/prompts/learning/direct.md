# Extraction steps

1. Align the NLQ meanings with their implementations in Gold. Identify all evidenced, reusable stored meanings, atomic calculations and requested SQL-generation decisions; a type may have no output.
2. Compare them with the semantic index and reuse matching knowledge. Read matching nodes for their definitions; inspect relevant dependencies or type-scoped node lists when the limited index cannot establish reuse.
3. Ground the knowledge in the supplied physical bindings. For a needed Value whose full identity is not known, read its owner Column and candidate Value nodes; match the owner, scalar and scalar type, then reuse the ID or add the missing Value. Investigate the database only for unresolved physical facts.
4. Reuse the supplied joins_with edges; do not create ordinary join Rules or speculative corrective Rules. Build complete semantic nodes and their necessary relationships. Express definition bindings and dependencies through refs; use explicit edges for additional or more precise evidenced relations. Do not change a formula or binding merely to obtain an unrelated edge.
5. Check coverage of supported NLQ meanings, consistency with Gold, and completeness of fields and references. Deliver the current graph for validation, accounting for all three semantic types, including empty types.
