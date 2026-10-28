from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, Field, field_validator

from cortex.capabilities.sql_training.core.enums import KeyRole, SemanticRole, UnitType
from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel, _uppercase


class ColumnStatistics(NodeModel):
    distinct_count: int | None = Field(default=None, ge=0)
    null_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    cardinality: str | None = None
    stats_updated_at: str | None = None


class ColumnProperties(NodeModel):
    references: list[str] = Field(default_factory=list)
    column_name: str
    source_metadata: dict[str, dict[str, str]] = Field(default_factory=dict)
    data_type: str | None = None
    sample_values: list[Any] = Field(default_factory=list)
    unit: UnitType | None = Field(
        default=None,
        validation_alias=AliasChoices("unit", "unit_type"),
    )
    nullable: bool | None = None
    semantic_role: SemanticRole | None = None
    key_role: KeyRole | None = None
    statistics: ColumnStatistics | None = None

    _validate_references = field_validator("references")(normalize_refs)
    _normalize_unit = field_validator("unit", mode="before")(_uppercase)
    _normalize_semantic_role = field_validator("semantic_role", mode="before")(_uppercase)
    _normalize_key_role = field_validator("key_role", mode="before")(_uppercase)
