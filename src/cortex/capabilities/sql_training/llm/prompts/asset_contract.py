from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal

ONTOLOGY_ASSET_DEFINITION_VERSION: Final = "database_ontology_assets_v66_business_roles"

CONCEPT_DEFINITION: Final = "A Concept expresses one minimal, complete, reusable business meaning supported by the case. It may describe an object, attribute or category; choose from meaning, data and evidence rather than a fixed role taxonomy. Targets are representations, not SELECT lists: groups imply neither select-all/one/coalesce nor output formatting. Every case-level Concept needs nonempty Column/Value targets; no Table targets or Concept-to-Table grounded_on. Table grain and coverage belong in metadata. Prefer Question terms. Identifier/name may represent one object in different roles; explain roles without assuming interchangeability or requiring separate Concepts. Keep names, definitions, tags and incident relation roles consistent with that business referent. A full-name field group can represent a person/role without making the Concept a name attribute or proving unique person identity. A supported business category may be a subclass even when defined by an attribute restriction; do not require extra attributes, relations, actions, documents or Rules beyond the case. Unfolding a category definition alone is not an NL split. Choose class or attribute meaning from the Question, SQL and available database evidence; neither overlap nor changeability decides this. A unique row key establishes record identity, not a homogeneous business-object type; describe mixed record kinds accurately rather than equating every row with the requested object. A stored business quantity remains a Concept; only a defining business calculation belongs to Metric. Different Concepts may share target inputs, but one physical endpoint may have a direct grounded_on connection from at most one Concept in the active graph."

CONCEPT_MATCHING_EXAMPLE: Final = """### Illustration: School

If the Question refers to schools and evidence supports these roles, retain **School** as the business Concept. Do not rename it School name merely because SQL counts the name column.

```text
School.targets = [[column.schools.CDSCode], [column.schools.School]]
School --grounded_on--> CDSCode: record identifier in this source.
School --grounded_on--> School: name representation; not necessarily unique or present.
```

Explain roles in the Concept and edge descriptions. These are two matching representations of one object, not a joint key or interchangeable count operands. Create a separate School name Concept only when the name attribute has an independently relevant meaning. Use actual Question terms and verified catalog IDs; this illustration is not a case-specific extraction rule.
"""

EVENT_DEFINITION: Final = "An Event expresses a business occurrence type, not an individual record or current state. Create it only when the evidence jointly identifies what occurs, participating objects and their roles, and a related occurrence time or place. An event word, timestamp, status value or JOIN alone is insufficient. Preserve only supported structure; do not invent workflow, lifecycle, direction, stages or instance grain. A participant's recorded/current location does not prove the historical event location; explain query scope without asserting unsupported occurrence geography."

EVENT_BINDING_RULE: Final = "Event targets, when supplied, identify supported physical matching groups; do not disguise an isolated timestamp, identifier or non-NULL test as a complete occurrence definition. A supported Event may instead be anchored through explicitly related, bound participant and time/place Concepts. Its description and saved relations must establish the actual event structure; an empty target list alone never justifies an empty-shell Event or event counting."

RELATION_SCOPE_RULE: Final = "Name semantic predicates for business roles, not field storage. has_attribute is an abstract attribute base, never an authored edge predicate. Reuse or define a concrete business predicate; optional extends=has_attribute declares specialization. Generate every edge explicitly with properties.description. For semantic edges, explain the business relationship first; storage-only prose is insufficient. Grounding/catalog edges may explain physical roles. Add only material supported constraints and record/object/time scope; do not invent a disclaimer for every edge. Keep profiling counts, code lists, probe SQL, proof narratives and repeated endpoint definitions in metadata/evidence. A record key does not prove lifetime object identity. Type-level business links do not imply instance existence, universal participation, cardinality, non-null values, JOIN permission or reverse implication. Unqualified scope is the current database and active definition, not all history. Distinguish intrinsic knowledge from current request filters and preserve source disagreements. Attribute predicates connect the direct business bearer to its attribute-role Concept. Storage in an object's table does not establish ownership; relations do not transitively transfer attributes. Prefer the evidenced applicable subtype when an attribute belongs to that subtype, without inventing universal availability or exclusive ownership. is_a is same-kind class inclusion, not necessarily a strict or immediate subclass; no extra-knowledge admission test applies. Shared parents imply neither sibling disjointness, overlap nor exhaustive coverage. category_of connects an attribute category/value meaning to its dimension, not the population satisfying it. An attribute slot and its value are not superclass/subclass. Keep endpoint meanings consistent, and never use description to contradict the predicate."

COMPOSITION_RULE: Final = "Composition links supported business objects or occurrences as part and whole; it is not subclassing, numeric comparison or a ratio numerator. Same-table fields, foreign keys and co-occurrence do not by themselves establish hierarchy, composition or causality. Related roles should be organized consistently around their core Concept without inventing mutual exclusion or complete coverage. A coded value/category is not a component part of its attribute slot or code-domain Concept. Numeric counts are not sets: explain any subset relation between the populations they count, not between the numbers themselves."

METRIC_ATOMICITY: Final = "Metric and Concept minimality concern NL meaning: reject a candidate decomposable into two or more complete Concept, Metric or NL-term meanings, each with a distinct independently identifiable field/value/operation match, whose composition preserves the whole meaning. The units need not already exist as nodes; cite them, their matches and composition. This is not word splitting, formula expansion or counting targets/operators. One term may need joint fields or a coupled formula; target overlap is allowed. A candidate's own name, definition, formula or unit cannot establish NL indivisibility. Distinguish a business term's defining calculation from a request operator applied to an independently matched term: nesting operators or restating the request as a definition does not make that composition atomic. Metric additionally requires a real defining calculation: a stored field, formatting cast or field * 1 is insufficient. Generic request aggregation over a complete population is an operation, not a new Metric. Preserve intrinsic population, units, grain, constants and coupled operations, including intrinsic normalization, division and time base; do not invent helper Metrics or turn every separated operation into a Rule. For example, average number of orders separates into Order + count + averaging; a supported profit margin has its defining numerator/denominator meaning. These are semantic examples, not keyword rules. Placeholder names do not compute quantities: binding count_x to an object Concept does not make it a count. Required operations belong in expression, not only a plan. Name, definition, pattern and formula must agree."

COUNT_SEMANTICS_RULE: Final = "Preserve the exact counted operand, DISTINCT grain and NULL behavior. COUNT(nullable_field), COUNT(*) and COUNT(CASE WHEN predicate THEN 1 END) are not interchangeable without evidence; equality on current rows does not establish semantic equivalence. A missing label does not prove a missing business object; non-NULL does not mean nonempty. Do not invent zero-denominator, imputation or default-value policies."

RULE_DECISION_BOUNDARIES: Final = """### Matching and use

- Concept owns static term-to-field/value mappings. Rule owns an evidenced choice beyond them; multiple targets alone need no Rule.
- Content selection chooses fields/groups/represented objects. Presentation holds that content fixed and changes its format/order. NLQ mentioning a choice does not determine its category.
- Test whether selection, presentation or row filtering can change independently. Do not bury binding decisions in ranking/formatting; keep necessary operands. Ordinary NULL pass-through or absent filters need no extra Rule.
- Gold supports the case choice, not a universal primary role or formatting convention. Do not infer preference from field suffixes or invent conditions to hide competing bindings.
"""

RULE_ATOMICITY: Final = """A Generation Rule records one evidenced implementation decision beyond Concept matching or Metric formulas. Direct and Repair share registry and admission; Repair may use confirmed errors, never invented history.

Require a nontrivial choice between semantically consequential implementations. Ordinary field/value/calendar matches, join facts, equivalent SQL spellings, hypothetical alternatives and registry membership do not suffice. Explain the supported choice without invented alternatives.

Action is one minimal, complete, reusable generation behavior. Split independently applicable complete behaviors, not necessary steps. Preserve prerequisites, operands, grain, direction, NULL/tie semantics and executable detail; brevity and tag count do not prove atomicity. Parameterize incidental values and K; linked Concept/Metric names may clarify scope. Use a local SQL pattern when needed. An arbitrary maximum does not imply COUNT.

Reuse intents within scope; multiple tags are allowed. Extend only for an evidenced decision no existing intent covers; explain meaning, registry gap and consequential alternatives in reason.
""" + "\n" + RULE_DECISION_BOUNDARIES


AssetContractSection = Literal[
    "common", "owners", "concept", "event", "metric", "generation_rule",
    "training_construction",
]

COMMON_ASSET_RULES: Final = """## Common Asset Rules

- Combine Question meaning, Gold implementation, supplied evidence, business knowledge and relevant database facts. Common sense proposes candidates; it does not override evidence or establish unsupported business rules.
- Preserve intrinsic knowledge and complete matching conditions. Leave request-specific dates, locations, thresholds, K and aliases as runtime inputs unless they define an evidenced stable business term.
- Use exact existing or jointly created graph IDs. Values must be verified through the governed path. SQL uses actual catalog identifiers, not graph IDs.
- Prefer existing identities and predicates. A field, value or phrase need not become a Concept merely because it occurs in the case. Keep nodes minimal without omitting distinctions needed to correct an observed error.
- Tables, Columns and governed Values are physical facts. No Entity, Process, Constraint or qualifier field is introduced. Database investigation is read-only and does not update metadata.
"""

OWNER_BOUNDARIES: Final = """## Owner Boundaries

- Concept defines one supported business meaning with nonempty physical matching groups.
- Event defines a supported occurrence structure with participants and occurrence context.
- Metric has one minimal NL meaning and an actual defining calculation; stored inputs are Concepts.
- Generation Rule defines a reusable generation behavior, not a static business limitation.

Choose from business meaning and evidence, not SQL syntax or a domain-specific role decision tree. A useful graph need not contain every type. Explicit edges carry relationships; targets and formula bindings are not substitutes for saved edges.
"""

CONCEPT_CONTRACT: Final = """## Concept Contract

""" + CONCEPT_DEFINITION + """

### Minimal meaning and matching

- Test NL meaning: can two or more complete Concept/Metric/NL-term units separately match fields, values or operations and reconstruct the whole meaning? If yes, split. Cite the units, their distinct matching roles and composition; they need not already exist as nodes. Joint targets for one meaning and overlapping targets do not prove decomposability. Do not confuse definition expansion with NL composition.
- `targets` is a nonempty list of nonempty matching groups: `[[A, B], [C]]` contains two matches, with A and B jointly forming the first. Each element is an exact Column or governed DimensionValue ID. Outer items may realize one meaning through distinct roles such as identifier and name; describe those roles. Inner members are jointly necessary for one match, such as a composite identifier. Co-occurring attributes do not form a joint match.
- Use optional `target_expressions` keyed by the zero-based group index, such as `"0"`, when a group needs a complete executable predicate or correlation expression. Preserve conditions; a list of columns alone does not explain their Boolean combination or JOIN.
- Multiple matching groups do not automatically authorize source substitution, JOINs or union. Explain their evidenced roles and differences in the description.
- Targets may overlap between Concepts. The graph's uniqueness rule concerns direct Concept-to-physical grounded_on connections, not occurrence counts in targets. One Concept may directly connect to several physical endpoints.
- A category matched by one governed Value grounds on that Value, whose value_of edge identifies its Column. Do not add a duplicate direct Column/filter edge for the same equality; a genuine distinct field dependency needs its own explanation.
- Explain an identifier's business identity or a Value's category meaning; keep table grain in metadata. Do not invent empty predicates, unknown keys or constant-true expressions to satisfy required bindings.

""" + CONCEPT_MATCHING_EXAMPLE + """

### Explicit connections

Write grounded_on edges explicitly with descriptions. Do not expand every target reference into a direct edge. If another Concept already directly connects to that physical endpoint, preserve matching information and express a supported Concept relationship rather than creating a duplicate connection or inventing an arbitrary relation. Same target inputs alone do not prove semantic equivalence.
"""

EVENT_CONTRACT: Final = """## Event Contract

""" + EVENT_DEFINITION + """

- Require a nonempty description of what happens and the supported relationship between participants and occurrence context. Those details must be linked to the same event, not merely present in the same Question.
- `targets` uses the same matching-group format as Concept and may be empty when explicit relations to bound Concepts establish the event structure. Optional `target_expressions` preserve necessary complete conditions, not the request's current date filters.
- Generate specific role relations, such as evidenced sender/recipient links, instead of using one undifferentiated has_participant for every role. Each edge needs its concrete explanation; do not add a role or qualifier field.
- Do not infer one-event-per-row, countability, sequence, causality, required participation or complete histories from keys, timestamps or JOINs.

""" + EVENT_BINDING_RULE + "\n"

METRIC_CONTRACT: Final = """## Metric Contract

""" + METRIC_ATOMICITY + """

### Calculation and operands

- `expression.expr` contains the complete formula with `${key}` placeholders. `expression.refs` maps each key to one exact Concept, Metric or supported physical ID; no missing or unused bindings.
- Use Concepts for named stored inputs. Inline unnamed intermediate calculations; do not create helper metrics solely to move formula pieces into other nodes.
- `pattern` states the same formula. Optional `sql_generation_plans` have local id/application/steps implementing only that calculation, with supported unit, population, source alignment and cardinality.
- Preserve intrinsic casts, scaling, operation order and time base. Leave separately requested filtering, ranking and display outside the Metric definition.
- There is no general `references` field. Explicit depends_on edges connect direct semantic operands; grounded_on edges describe direct physical inputs. Do not replace a direct formula edge with an indirect path or add transitive operands. One operand may occur in multiple formula positions; explain its roles on one edge. Plan prose alone does not create dependencies.

""" + COUNT_SEMANTICS_RULE + "\n"

GENERATION_RULE_CONTRACT: Final = """## Generation Rule Contract

""" + RULE_ATOMICITY + """

### Definition and tags

- `condition` states all runtime prerequisites for the action; names, tags and the current case cannot supply omitted scope. Keep it minimal, not a whole-Question summary or chosen SQL representation.
- `action` states the selected minimal behavior clearly enough to implement. Include a short local SQL pattern when it clarifies the decision; resolve operands through linked Concepts/Metrics and explain runtime placeholders. Do not copy the whole query.
- `reason` explains an evidenced consequence without inventing cardinality or excluding equivalent implementations.
- `operation_intents` reuses fitting registry labels without exceeding their defined scope. Multiple fitting labels are allowed. A new intent requires an evidenced implementation decision absent from the registry; explain its meaning, the registry gap and consequential alternatives in reason. A business name, parameter or equivalent SQL spelling is not a new intent.
- Direct and Repair use the same complete registry. Direct uses case/Gold/database evidence; Repair may additionally use confirmed errors. Do not invent error history. Ordinary field/value matches, date filters or join facts alone do not justify a Rule.
- `triggers` provides retrieval signals, not applicability proof. Direct Question phrases must be supported by the Question and SQL-side triggers remain empty; Repair may use observed error signals.
- There is no general `references` field. Write applies_to and any necessary supported connections explicitly, omitting unrelated runtime values and co-occurring nodes.

A Rule does not replace a Concept binding or Metric formula. A confirmed error can justify a corrective Rule alongside correct definitions. A static limitation, such as a date possibly being missing, belongs in the relevant relationship description rather than a fabricated generation action.
"""

SQL_TRAINING_CONSTRUCTION_RULES: Final = """## SQL Training Construction Rules

Publish interdependent nodes, matching groups and explicit edges together in one atomic edit. Calculation dependencies must be acyclic; business relationships are not all calculation dependencies. Multiple valid graph organizations are allowed; minimize node content, not graph size. Remove only proven duplicate assertions or assertions derivable by declared rules in the same scope, preserving evidence and required consumer links. An alternate path alone is not proof. Do not contract Events or search for a minimum graph. Edges are saved facts, not inferred from general refs. Optional deterministic missing-edge completion may use unambiguous retained formula/catalog references, but must not guess business predicates or expand overlapping targets into duplicate direct connections.

Direct builds supported knowledge. Repair may revise any semantic type or relation to resolve fixed Diagnosis/Assessment feedback while preserving accepted behavior. Diagnosis does not draft assets or edit the graph. The controller validates, assesses and accepts or reverts the tentative revision. Correct SQL does not validate unsupported semantic edges.

Instructions and schema descriptions use English. Asset names, descriptions, patterns, plan prose, Rule conditions/actions/reasons and Question triggers follow the Question's primary language. Graph IDs, SQL identifiers and operators retain their technical format.

Use business context and metadata first; investigate the database when material ambiguity or a claimed limitation requires it. Do not infer a permanent join type, role, lifecycle or global data invariant from one query. Preserve known disagreements and narrow statements to the evidence. No metadata optimization or separate exploration-evidence subsystem is required.

There is no type quota or knowledge-coverage score. Review the actual nodes/edges for defects and use observed errors to identify missing knowledge. Operation intents guide action boundaries and retrieval; they are neither a creation quota nor evidence of correctness.
"""

_SECTION_TEXT: Final[dict[AssetContractSection, str]] = {
    "common": COMMON_ASSET_RULES,
    "owners": OWNER_BOUNDARIES,
    "concept": CONCEPT_CONTRACT,
    "event": EVENT_CONTRACT,
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
        "This contract defines semantic asset meaning and graph-authoring requirements. "
        "The runtime schema owns exact field shapes; stage instructions govern evidence and delivery.\n\n"
        f"{rendered}\n"
    )


ONTOLOGY_ASSET_DEFINITIONS: Final = render_ontology_asset_contract(
    ("common", "owners", "concept", "event", "metric", "generation_rule")
)

__all__ = [
    "COMMON_ASSET_RULES",
    "COMPOSITION_RULE",
    "CONCEPT_CONTRACT",
    "CONCEPT_DEFINITION",
    "CONCEPT_MATCHING_EXAMPLE",
    "COUNT_SEMANTICS_RULE",
    "EVENT_BINDING_RULE",
    "EVENT_CONTRACT",
    "EVENT_DEFINITION",
    "GENERATION_RULE_CONTRACT",
    "METRIC_ATOMICITY",
    "METRIC_CONTRACT",
    "ONTOLOGY_ASSET_DEFINITIONS",
    "ONTOLOGY_ASSET_DEFINITION_VERSION",
    "OWNER_BOUNDARIES",
    "RELATION_SCOPE_RULE",
    "RULE_ATOMICITY",
    "SQL_TRAINING_CONSTRUCTION_RULES",
    "AssetContractSection",
    "render_ontology_asset_contract",
]
