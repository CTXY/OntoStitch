# Explicit relations

Generate every required relationship as an explicit edit_graph.edge_operations item. Endpoints must already exist or be created in the same edit. Read inspect_graph(mode="relations", edge_type=...) for saved relation contracts and exact endpoint constraints. Nodes, matching groups, formulas and edges are distinct structures; do not assume refs will build the graph for you.

## Eight core relations

- grounded_on connects a semantic node to its actual physical realization or input. Concept targets are Columns/Values only; Table grain and coverage belong in metadata. Other semantic types follow their own contracts. Explain the actual physical role, not a catalog summary.
- depends_on connects a Metric to a Concept or Metric used in its calculation. Explain each operand's actual role. General association is not calculation dependency.
- value_of connects a governed DimensionValue to its owner Column; belongs_to connects a Column to its Table. Code owns physical catalog writes.
- joins_with stores evidenced physical table join keys, not arbitrary business association; preserve complete key pairs and grain.
- has_attribute is an abstract base; never emit it as an edge. Its concrete specializations connect a Concept/Event object type to a Concept expressing its business attribute. It is not a physical Column link or a selected population; no non-null, functional or exhaustive-value constraint is implied.
- is_a states same-kind class inclusion, not an immediate parent, proper subset or object-attribute link.
- applies_to connects a GenerationRule to a supported semantic object when useful. The condition governs applicability and combinations of targets; the edge does not automatically inherit along is_a or composition.

## Business predicates are optional

The core includes belongs_to, value_of, joins_with, grounded_on, depends_on, applies_to, is_a and has_attribute. Other predicates are not mandatory taxonomy. New business predicates are explicitly allowed. Reuse a saved contract when its meaning fits, or submit a new relation_contracts definition and its edge_operations in the same edit. Core relations are a starting vocabulary, not an exhaustive whitelist. Names such as category_of, part_of and event roles below illustrate possible meanings, not pre-registered permissions or required graph structures.

Do not author computed_from, filters_on, expands_to, related_to or aligns_with: these are read-only historical names. Use grounded_on for direct physical operands, full target/formula definitions for fixed predicates, and a precise business relation for a supported connection. A query-specific aggregation/alignment decision belongs in a qualifying GenerationRule or formula plan, not an unconditional table edge. Preserve extra restrictions when migrating; never mechanically rename all historical edges.

Use a supported business relation, such as is_a, has_email_address, opens, paid_from or paid_to, when its meaning fits. New predicates require a saved EdgeType contract in GraphSpace.relation_contracts, submitted through edit_graph.relation_contracts with the edges. Existing same-name contracts are immutable. Define meaning, direction, valid endpoint roles and material limits; do not weaken a core contract. The edge description explains this particular connection. Reuse synonymous predicates instead of creating a name for every edge.

- is_a means non-strict class inclusion: Concept→Concept or Event→Event, never a cross-type pair. It does not assert an immediate parent or a proper subset. Both ends must express comparable classes: a category recorded in an attribute is not a subclass of the attribute slot. It is not attribute, location, part-whole or foreign-key association. Do not default to broader_than.
- has_attribute is an abstract attribute base. Every actual attribute edge requires a concrete business predicate and saved contract; do not emit has_attribute. It means that an attribute applies to the object type; it does not by itself require every database record to contain a known value.
- category_of connects a dimension category/value meaning to its attribute/dimension Concept. An object population satisfying a value is not that value meaning; do not use this relation to conceal the distinction. It is neither is_a nor physical value_of.
- Use specific event-role predicates when roles differ. One generic has_participant label cannot explain sender versus recipient. A foreign-key reference alone does not establish a participant role.
- Composition needs actual part-whole meaning. Two dates do not prove temporal order, and a JOIN does not prove a workflow or lifecycle.

{composition_rule}

## Sibling categories

Shared is_a parents alone imply neither disjointness, overlap nor complete coverage. Add only supported statements with scope and class identities; no need to invent missing siblings or all pairwise links. Different fields alone do not prove unrestricted compatibility; no overlap in a database sample does not prove disjointness.

## Description is required

{relation_scope_rule}

Every edge needs properties.description, usually one or two sentences: the concrete relationship plus any necessary supported constraint. State cardinality/optionality, conditions, time or population scope when material. Distinguish a record key from a business object's lifetime identity, and missing recorded attributes from missing objects. Do not invent a constraint merely to fill the description.

Keep field types, profile counts, value lists, SQL probes and proof narratives in metadata/evidence. Do not repeat endpoint definitions or append unrelated disclaimers. Use precise predicates and description; no qualifier, role or Constraint field/node is added.

A type-level role/attribute edge does not automatically create instance facts, non-NULL predicates, target inheritance or JOIN execution. Only declared rules permit inference; is_a inclusion is transitive, while direct formula depends_on is not. Description cannot contradict the relation type: an is_a edge cannot actually mean located_in.

Do not use related_to to hide a rejected predicate or merely record non-equivalence. Put supported limits on the actual object/attribute connection. Dates and status need not imply one another; a date belongs to the object or event, not its category value.

## Matching and direct-edge uniqueness

Different Concepts may share physical IDs in targets. Only their actual direct grounded_on connections are subject to the one-Concept-per-physical-endpoint rule. One Concept may directly connect to several endpoints. Do not turn every matching-group item into a direct edge or infer a business relationship solely from overlap.

Concept grounded_on endpoints must occur in the Concept's matching groups. Preserve the complete conditions and source scope; an edge does not turn one member of a joint match into a sufficient binding.

Generate complete edges yourself. Optional code completion can only fill deterministic omissions from retained formula/catalog references; it cannot guess is_a or event roles. Do not rely on it to author descriptions or semantic structure.

## Whole-graph checks

- Default to the current database and active definition scope. Narrower time/condition scopes belong in description; missing scope never means all history or all records.
- Check definition/binding agreement, endpoint roles and same-occurrence correlations. Keys alone do not prove business identity or event grain.
- Remove duplicates only with matching meaning and scope; preserve provenance/limits and surface conflicting descriptions. Derivable assertions require a remaining witness and declared rule, such as same-scope is_a transitivity. Shared targets, alternate paths or equal SQL results are not proof.
- Before deletion, preserve direct formula/catalog links and verify consumer support. Retain unsupported materializations and report the limitation. Recheck witnesses after each deletion; never remove mutually supporting facts together.
- Review is_a cycles for equivalence/errors; reject calculation cycles, not all graph cycles. Do not optimize graph size, contract Events or require a unique hierarchy.

## Edge operation example

Illustration only: the evidence establishes Priority orders as the Order members satisfying an evidenced priority designation, and both Concepts already have valid matching targets.

```json
{
  "operation": "create_edge",
  "edge": {
    "id": "edge.priority_order_is_order",
    "edge_type": "is_a",
    "from_node_id": "concept.priority_orders",
    "to_node_id": "concept.order",
    "properties": {
      "description": "Priority orders are the Order members with the evidenced priority designation; this does not imply exclusion from independently defined order categories."
    }
  }
}
```

Use update_edge with the full edge, or delete_edge with edge_id. Reuse an edge ID for a revision of the same fact. Same endpoints and type do not alone prove two differently scoped statements equivalent; do not silently overwrite scope. Revert tentative edges with revert_ids. Never submit system provenance, reference_origin or alternative_ids; code maintains them. Every atomic edit is checked as a combined graph, then assessed for business truth.

Numeric counts are not populations. Describe how the counted populations relate, without asserting set inclusion or calculation dependency between stored numbers. Use the actual predicate and description; do not add a relation_hint as a competing predicate.

## Attribute family and group statements

Use a supported concrete business predicate; has_attribute itself is not an allowed edge. A new attribute contract may set `extends: "has_attribute"`; only this one-level specialization is supported. Its endpoints/direction cannot widen the root contract. Semantic specialization also requires child(a,b) ⇒ has_attribute(a,b), preserving both endpoint roles; matching JSON node types alone is insufficient. A category-to-dimension membership relation is independent, not object-to-attribute specialization. Keep concrete business meaning in its own description. Do not save a duplicate has_attribute edge. located_in and other non-attribute roles may remain independent predicates.

A Concept may carry optional properties.relation_metadata. Each item has exactly id, edges and description. Use at least two distinct existing edge IDs, all adjacent to that Concept with identical predicate and relative direction. Inspect its node for both incoming and outgoing edges. Read an incoming predicate contract by edge_type without a node_id scope. This example is illustrative, not a required classification:

```json
{"id":"independent_categories","edges":["edge.category_a_is_object","edge.category_b_is_object"],"description":"These categories describe different business dimensions; membership in one does not imply or exclude the other."}
```

Save new edges and their owner metadata in the same atomic edit. Only list supported group knowledge; omission is allowed. The group applies only to its listed edges, not future neighbors. Keep scope and distinguish no exclusion claim, allowed co-membership, observed overlap and exhaustive coverage. No fixed disjoint/compatible flags, automatic pairwise edges or new Constraint nodes. Changing/deleting a referenced edge may require updating the owner in the same edit. This field is not SQL metadata and does not create inference or retrieval expansion.

For an attribute with an evidenced named business role, name that role in a specific predicate and optionally extend has_attribute. If no concrete role can be supported, investigate or omit the unsupported edge; do not fall back to has_attribute. A description that merely says the target is an attribute does not supply that role.

## Direct business bearer

Connect an attribute to the object or evidenced subtype it directly describes. A table can store several related objects; storage location is not attribute ownership. Object relationships do not transfer attributes transitively. Keep endpoint names, definitions and roles consistent. Cross-level edges are allowed when independently meaningful; remove duplicates only under established contracts and scope.
