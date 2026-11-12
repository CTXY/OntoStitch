"""Small authoring vocabulary; historical contracts are read compatibility only."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from cortex.schema.models import EdgeType

CORE_RELATIONS = frozenset({
    "belongs_to", "value_of", "joins_with", "grounded_on",
    "depends_on", "applies_to", "is_a", "has_attribute",
})
ABSTRACT_RELATIONS = frozenset({"has_attribute"})


def require_concrete_relation(predicate: str) -> None:
    """Historical graphs remain readable; new assertions need a concrete role."""
    if predicate in ABSTRACT_RELATIONS:
        raise ValueError(
            f"{predicate} is an abstract relation base. Reuse or define a concrete "
            "business predicate and verify its direct business bearer; "
            "optionally declare extends=has_attribute."
        )


RETIRED_RELATIONS = {
    "computed_from": "Use grounded_on for direct physical formula inputs; preserve any additional supported restrictions.",
    "filters_on": "Keep the full stable predicate in matching groups or the defining formula/input Concept; do not duplicate it with a field edge.",
    "expands_to": "State the actual supported business relation; generic expansion is not a relation meaning.",
    "related_to": "Use a precise evidenced predicate, or omit the edge when the relation is unknown.",
    "aligns_with": "A required aggregation/alignment strategy belongs in a scoped generation decision or formula implementation, not an unconditional table link.",
}


@lru_cache(maxsize=1)
def legacy_relation_contracts() -> tuple[EdgeType, ...]:
    """Preserve historical meanings without registering them for new authoring."""
    path = Path(__file__).with_name("roots") / "legacy_relations.json"
    return tuple(EdgeType.model_validate(item) for item in json.loads(path.read_text()))
