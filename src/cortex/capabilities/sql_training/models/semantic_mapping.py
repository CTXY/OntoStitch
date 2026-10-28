from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel


class SemanticMappingTarget(NodeModel):
    id: str = Field(
        description="Stable target-local identifier; not an ontology Node ID."
    )
    sql_expression: str = Field(
        description="Complete SQL expression implementing this Concept, possibly across multiple tables. Preserve necessary correlation and conditions; use actual catalog identifiers rather than ontology IDs or plan placeholders."
    )
    references: list[str] = Field(
        default_factory=list,
        description="All physical Column and governed Value IDs used by this expression.",
    )

    @field_validator("references")
    @classmethod
    def validate_references(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_refs(values)))

    @model_validator(mode="after")
    def validate_target(self) -> SemanticMappingTarget:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", self.id):
            raise ValueError(
                "Concept target id must start with a letter and contain only letters, digits, or underscores"
            )
        if not self.sql_expression.strip():
            raise ValueError("Concept target sql_expression must be non-empty")
        if any(
            not ref.startswith(("column.", "dimension_value."))
            for ref in self.references
        ):
            raise ValueError(
                "Concept target references must be physical Columns or governed Values"
            )
        return self


class ConceptProperties(NodeModel):
    mapping_type: str | None = Field(
        default=None,
        description="Optional mapping classification; it does not define Concept identity.",
    )
    targets: list[SemanticMappingTarget] = Field(
        default_factory=list,
        description=(
            "Complete physical realizations of the same Concept, potentially across multiple "
            "tables. Each target separately implements the meaning; compatibility is assessed externally."
        ),
    )

    @model_validator(mode="after")
    def validate_mapping(self) -> ConceptProperties:
        fingerprints = [
            (
                target.sql_expression.strip(),
                tuple(sorted(target.references)),
            )
            for target in self.targets
        ]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Semantic Mapping contains duplicate targets")
        target_ids = [target.id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Semantic Mapping contains duplicate target ids")
        return self
