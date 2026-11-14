"""Type-based semantic review requirements, independent of cases and SQL literals."""

QUALITY_CRITERIA = {
    "type_fit": "Content fits its type under the shared contract; calculation syntax alone does not make a Metric.",
    "minimality": "Apply the shared NL split test. A failure identifies complete units, distinct matches and meaning-preserving composition. Do not confuse identifier/name representations or category-definition expansion with independent meanings; lack of an indivisibility proof is not failure.",
    "grounding": "Matching groups have valid Column/Value meanings and complete supported conditions. Joint members are necessary; a Value already identifies its Column. Explain representation roles and identity scope without assuming unique names or interchangeable operands.",
    "metric_minimality": "Apply the same NL split test to name, definition and implementation. Distinguish a request operator over an independently matched term from defining coupled calculation. A failure gives units, matches and composition; formula expansion alone is insufficient.",
    "formula_scope": "expression contains the complete defining calculation and actual operand meanings, not only a plan or generic request operations. refs, pattern, units, population, grain and implementation agree; placeholder names do not compute counts.",
    "decision_value": "An evidenced consequential implementation choice beyond static matching/formulas. Registry membership, ordinary operators, literal/calendar filtering, equivalent SQL spellings and hypothetical alternatives alone are insufficient. Check fitting labels and justified gaps; matching groups alone do not imply selection/presentation policy.",
    "atomicity": "One minimal complete reusable action. On failure identify independently applicable complete decisions and conditions; do not split necessary steps. Distinguish content selection, presentation and row filtering.",
    "abstraction": "Parameterize incidental values/K; retain intrinsic constants, necessary operands and business scope. Linked Concept/Metric names are allowed.",
    "condition_action": "Prerequisites and concrete implementable action agree. Local SQL patterns preserve supported operands, direction, grain, cardinality and NULL/tie semantics; no invented policies.",
    "structure": "Occurrence, participant roles and related occurrence time or place are jointly supported; do not confuse current participant location with event geography or invent countability.",
    "endpoint_meaning": "Endpoint referents fit the relation roles. Distinguish object, attribute, dimension category and filtered population. extends must entail the parent meaning with unchanged roles, not merely matching JSON node types.",
    "predicate_precision": "Use a supported role-specific business predicate and supplied contract. has_attribute is abstract. Descriptions cannot replace a missing contract or disguise storage-only semantics; physical links retain their physical role.",
    "scope": "Describe the actual relationship and material evidenced conditions/limits. No invented obligatory NULL disclaimer, cardinality, metadata dump, probe SQL or repeated endpoint definition. Preserve record/object/time scope.",
    "support": "Apply the shared evidence policy: concrete contract violation, same-scope contradiction or material loss, not mere missing wording/unselected endpoints. Mandatory admission/binding requirements remain. Scoped omission does not establish absence.",
    "graph_consistency": "Compare supplied definitions, bindings, formulas, roles and scope; cite exact incompatible claims/IDs. Review overlapping Rule conditions/shared decisions. Distinguish complementary/conditional meanings from conflicts; no coverage or graph-size requirement.",
    "assertion_nonredundancy": "A failure needs equivalent same-scope meaning or a declared rule with independent remaining premises, preserved limits and safe consumer consolidation. Preserve direct formula/catalog links. Unproved/unsafe redundancy and structural signals alone are not failure.",
    "removal_safety": "Deletion preserves supported behavior and leaves dependent knowledge valid."
}


def required_quality_checks(candidate, *, relation=None):
    if candidate is None:
        return ["removal_safety"]
    if relation is not None:
        return ["endpoint_meaning", "predicate_precision", "scope", "support", "graph_consistency", "assertion_nonredundancy"]
    kind = candidate.get("node_type")
    checks = {
        "concept": ["type_fit", "minimality", "grounding", "support"],
        "metric": ["type_fit", "metric_minimality", "formula_scope", "support"],
        "event": ["type_fit", "structure", "support"],
        "generation_rule": ["type_fit", "decision_value", "atomicity", "abstraction", "condition_action", "support"],
    }.get(kind, ["type_fit", "support"])
    return [*checks, "graph_consistency"]
