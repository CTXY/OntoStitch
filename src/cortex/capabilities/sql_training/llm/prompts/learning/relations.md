# Explicit relation authoring

Apply the shared relation contract. Nodes, targets, expression.refs and edges are separate structures: author all semantic edges explicitly through edge_operations. Endpoints must exist or be created in the same edit.

Use the lightweight relation index to locate predicates; read edge_type for its full endpoint/meaning/inference contract when needed. Reuse fitting contracts. New business predicates are allowed: submit relation_contracts with their edges, and do not overwrite same-name contracts or weaken core meanings. Retired predicates are read-only; migration must preserve additional restrictions, never mechanically rename.

For each edge, properties.description explains the actual business relationship and material supported constraints, usually one or two sentences. No compulsory nullable disclaimer, metadata dump, proof narrative or repeated endpoint definition. Physical grounding/catalog links explain representation roles. Use concrete business predicates; never emit has_attribute or use related_to to hide an unsupported assertion.

A new attribute predicate may set extends=has_attribute only when it preserves object-to-attribute meaning and endpoint roles, not merely JSON node types. Location, part-whole and category-to-dimension roles need their own supported meanings. Do not create a duplicate abstract-base edge. Attribute ownership follows the direct business bearer/evidenced subtype, not storage or transitive object links.

Use create_edge/update_edge with the schema-required edge and delete_edge with edge_id. Preserve an edge ID when revising the same fact; same type/endpoints do not prove equivalent scope. Save connected changes coherently. Do not submit code-maintained provenance, reference_origin or alternative_ids.

## Group statements

Optional Concept properties.relation_metadata contains items with id, edges and description. Reference at least two distinct existing adjacent edges with identical predicate and relative direction; inspect both incoming/outgoing when necessary. Read an incoming predicate by edge_type without node_id scope.

Statements apply only to listed edges and supported scope, not future neighbors. Distinguish no exclusion claim, allowed co-membership, observed overlap and exhaustive coverage. Do not require pairwise edges or invent fixed disjoint/compatible flags. New or deleted referenced edges and owner metadata must remain valid in the same edit. This metadata creates no SQL execution or automatic inference.

## Deletion

Prove duplicate meaning/scope or derivability under a declared rule with independent remaining witnesses; preserve restrictions, provenance and current consumers. Do not remove mutually supporting assertions or substitute indirect paths for direct operands/catalog ownership. Review is_a cycles for equivalence/errors; shared targets and arbitrary triangles alone are not defects. No graph-size objective applies.
