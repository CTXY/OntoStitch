from __future__ import annotations

from collections.abc import Iterable
from typing import Final, Literal

ONTOLOGY_ASSET_DEFINITION_VERSION: Final = "database_ontology_assets_v30"

CONCEPT_DEFINITION: Final = "A Concept represents a stored meaning explicitly named in the NLQ and binds it to its correct physical implementation. It may be a calculation input, a filter meaning or a requested output. A named input remains a Concept when used in a calculation. A result requiring a formula belongs to Metric; one input field cannot stand in for that result."

METRIC_ATOMICITY: Final = "A Metric defines one atomic named calculation. Its formula may contain several operations that jointly define the quantity. Applying a separately requested query operation to an already complete Concept or Metric does not by itself create another Metric. Decide from the business meaning, not the SQL function or the number of operators."

RULE_ATOMICITY: Final = "A Generation Rule expresses one atomic, reusable SQL-generation decision. Separate independently useful operation intents; keep multiple operations together only when they jointly implement an inseparable decision. Separate current request values and unrelated outputs from the condition and action; retain fixed facts only when the decision requires them. A registry match permits extraction but does not require a Rule for every SQL operation."

AssetContractSection = Literal[
    "common",
    "owners",
    "concept",
    "metric",
    "generation_rule",
    "training_construction",
]

COMMON_ASSET_RULES: Final = """## Common Asset Rules

- Extract reusable knowledge supported by the NLQ, Gold and supplied evidence. Gold establishes implementation, not additional unmentioned business meanings.
- Preserve facts intrinsic to the knowledge. Leave values supplied only for the current request as runtime inputs; omit temporary aliases, full queries, case IDs and replay traces.
- Use exact physical and Working node IDs, including nodes created together in one atomic edit. Reference only what the definition or behavior needs; co-occurrence is not a dependency.
- Keep the evidenced implementation without adding unsupported defaults or broader applicability.
"""

OWNER_BOUNDARIES: Final = """## Owner Boundaries

- Concept binds a named stored meaning to physical data, including named calculation inputs and requested outputs.
- Metric defines an atomic named calculation, with its complete formula.
- Generation Rule describes a reusable SQL-generation decision. An observed error may justify a corrective Rule alongside the Concept or Metric whose correct definition it uses.

Choose by the meaning being represented. Keep independently meaningful knowledge separate; sharing physical inputs does not make two meanings duplicates. SQL structure alone does not establish an ontology identity.
"""

CONCEPT_CONTRACT: Final = """## Concept Contract

""" + CONCEPT_DEFINITION + """

### Bindings

- Each target independently implements the whole named meaning and may jointly reference multiple physical tables. Jointly required inputs belong in one target; compatible alternate complete implementations use separate targets.
- Use qualified Columns and a complete physical expression or predicate, preserving necessary association logic. Multiple targets do not by themselves establish equivalence or a conditional selection policy. Preserve unresolved complete definitions when compatibility is not established. Do not invent a field or predicate merely because it exists in the catalog.
- A field and its named category are distinct meanings: bind the field to its Column and the category to its complete predicate.
- Thresholds, date boundaries and other values supplied only for the current operation remain runtime inputs. A stable named category or governed period may be a Concept.

### Target format

Each target contains a stable local `id`, a qualified executable `sql_expression` and exact physical `references`. Use catalog Table/Column names in SQL and canonical Column/Value IDs in references. Include every required physical input without unrelated nodes. Do not use query aliases, placeholders or constant-true predicates as bindings.

Target references generate `grounded_on` edges carrying that target's `target_id`. These edges record dependencies, not semantic equivalence.
"""

METRIC_CONTRACT: Final = """## Metric Contract

""" + METRIC_ATOMICITY + """

### Calculation

- Establish the named quantity from the NLQ and evidence; use Gold to recover its complete implementation. Do not invent identities from helper aliases or unnamed formula fragments.
- Preserve intrinsic operations, casts, units, scaling, population, grain and operation order. Keep separately requested filtering, grouping, ranking and display outside the definition.
- Named stored inputs may have their own Concepts. The Metric still defines the calculation rather than duplicating an input's binding.

### Expression and plan

- `expression.expr` contains the complete formula using `${key}` placeholders. `expression.refs` binds each key to an exact Column or independently identified Metric. Inline unnamed intermediate calculations.
- `references` contains exactly the dependencies used by those bindings. It does not include transitive dependencies or retrieval hints.
- `pattern` explains the same calculation. Each `sql_generation_plans` entry has a local `id`, `application` and ordered `steps` implementing that formula, including necessary source alignment and cardinality.
- Use fully qualified `<table.column>` tokens in plan prose and canonical IDs in expression refs. Plans must stand alone without copying the whole query, current literals or temporary aliases.

Metric dependencies generate `depends_on`; physical dependencies generate `grounded_on`. Plan-only tokens do not create formula dependencies.
"""

GENERATION_RULE_CONTRACT: Final = """## Generation Rule Contract

""" + RULE_ATOMICITY + """

A Rule does not replace a Concept binding or Metric formula. An observed error may justify a corrective Rule that uses those definitions.

### Definition

- `condition` states the smallest runtime-visible business situation requiring the decision, before choosing its implementation.
- `action` states the complete required behavior and necessary bindings. Include a local SQL pattern with explained roles only when prose leaves the implementation ambiguous.
- `reason` explains an evidenced or SQL-supported consequence, without assuming cardinality or excluding equivalent implementations. Contrast an evidenced wrong alternative when available; do not invent one for an omitted behavior.
- Keep operations together when they jointly implement one decision. Separate independently reusable decisions, rather than splitting by SQL clause or repeated occurrence.
- Keep current values, thresholds, dates, K, temporary aliases and full queries out of permanent Rule content. Preserve fixed facts required by the Rule's own meaning.

### References and retrieval

- `references` lists direct, jointly required dependencies of the condition/action. Prefer the semantic knowledge being used; reference physical nodes when the decision requires their specific bindings. Do not add transitive nodes, merely co-occurring nodes or runtime argument values as fixed dependencies.
- Question phrases and SQL Columns/operations are independent OR retrieval signals, not applicability tests or dependency edges.
- Rule references to Concept/Metric generate `applies_to`; physical references generate `grounded_on`.

### Extraction stage

- Direct uses one `operation_intent` from the supplied registry. Its condition and Question triggers are supported by the NLQ; Gold proves the action. SQL-side triggers stay empty. Ordinary physical join facts and speculative corrections are not Direct Rules.
- Repair may learn other behaviors from observed errors and record predictive SQL error signals. Correct binding knowledge and corrective behavior have separate roles and may coexist.
- Diagnosis fixes the repair targets. Learning and Assessment work within that scope rather than splitting it again.
"""

SQL_TRAINING_CONSTRUCTION_RULES: Final = """## SQL Training Construction Rules

Use exact IDs from the current physical and Working graphs. Concepts have physical bindings, not semantic dependencies. Generation Rules may depend on Concepts or Metrics; Metrics may depend on other independently named Metrics. Reuse existing identities and create only the missing minimal acyclic dependency closure. Publish dependencies before their dependents, or together in one legal atomic edit.

The Learning Agent can add missing semantic nodes, revise existing content and revert tentative changes during an episode. Existing IDs and types remain fixed. Diagnosis fixes repair targets and may recommend accepted base identities; it does not draft assets or edit the graph. The controller validates changes, accepts supported groups and restores the latest accepted snapshot when discarding tentative work. Rejected replacements must preserve earlier accepted versions.

Instructions and output-Schema descriptions are written in English. Asset natural-language content follows the Question's primary language: use an exact Question span for Concept names unless a dependent modifier must be completed with its exact Question-explicit bare entity; prefer the Question's named business phrase for Metric names; and write descriptions, patterns, plan prose, Rule names, conditions, actions, reasons, and Question triggers in that language. Canonical IDs, physical references, SQL tokens, and operators keep their required technical format.

Ordinary joins are physical joins_with facts, not Direct Generation Rules. Error-driven Rules may capture evidenced join corrections or nontrivial path/grain constraints. Do not infer a permanent join type from a single query.

Direct Generation Rule discovery uses the Initial Operation Registry available through the type guide. A Rule must match one listed operation, be explicitly requested by the Question, be directly implemented by Gold, and not be owned by a Concept or Metric. Error-driven repair may learn other Rule behaviors when the observed error supplies the missing evidence. Direct extraction records the matched Registry value in the direct `operation_intent` field; permanent Rules keep that field optional and do not use a nested metadata object.
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
    "COMMON_ASSET_RULES",
    "CONCEPT_CONTRACT",
    "CONCEPT_DEFINITION",
    "GENERATION_RULE_CONTRACT",
    "METRIC_ATOMICITY",
    "METRIC_CONTRACT",
    "ONTOLOGY_ASSET_DEFINITIONS",
    "ONTOLOGY_ASSET_DEFINITION_VERSION",
    "OWNER_BOUNDARIES",
    "RULE_ATOMICITY",
    "SQL_TRAINING_CONSTRUCTION_RULES",
    "AssetContractSection",
    "render_ontology_asset_contract",
]
