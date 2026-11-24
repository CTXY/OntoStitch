from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.schema.semantic_validation import validate_matching_targets

# One list is one jointly interpreted match, not a list of graph edges.
SemanticMappingTarget = list[str]


class ConceptProperties(NodeModel):
    mapping_type: str | None = Field(
        default=None,
        description="Optional mapping classification; it does not define Concept identity.",
    )
    targets: list[SemanticMappingTarget] = Field(
        min_length=1,
        description=(
            "Nonempty matching groups of canonical Table, Column or governed Value IDs. "
            "Members of each group jointly define one match. Different groups are separate "
            "matches, not an instruction to JOIN sources. Other Concepts may reuse these "
            "inputs; submit graph relations explicitly rather than expanding every target to an edge."
        ),
    )
    target_expressions: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional complete SQL expressions for complex matching groups. Keys are zero-based "
            "group indices as strings, such as '0'. Preserve joint predicates and correlations; "
            "direct Table/identifier/Value matches need no artificial SQL expression."
        ),
    )

    @field_validator("targets")
    @classmethod
    def normalize_targets(cls, groups: list[list[str]]) -> list[list[str]]:
        normalized = [normalize_refs(group) for group in groups]
        if any(not ref.startswith(("table.", "column.", "dimension_value."))
               for group in normalized for ref in group):
            raise ValueError("Matching targets must use physical Table, Column or governed Value IDs")
        return normalized

    @model_validator(mode="after")
    def validate_mapping(self) -> ConceptProperties:
        validate_matching_targets(
            self.targets, self.target_expressions, required=True, node_type="Concept"
        )
        return self


class ConceptAuthoringProperties(ConceptProperties):
    """New extraction writes; the base model still reads historical Table bindings."""

    targets: list[list[Annotated[str, Field(pattern=r"^(column|dimension_value)\..+")]]] = Field(
        min_length=1,
        description="Column/Value matching groups only. Outer items may realize one object through identifier/name roles; explain their uses, not automatic interchangeability. Inner members must be jointly necessary. Table grain and coverage belong in metadata.",
    )
