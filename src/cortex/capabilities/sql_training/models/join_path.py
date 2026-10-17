from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from cortex.capabilities.sql_training.core.enums import JoinCardinality, JoinType, NodeType
from cortex.capabilities.sql_training.core.ids import normalize_ref
from cortex.capabilities.sql_training.models.base import NodeModel, _uppercase


class JoinCondition(NodeModel):
    left_column_ref: str
    right_column_ref: str
    operator: str = "="

    @field_validator("left_column_ref", "right_column_ref")
    @classmethod
    def _validate_column_ref(cls, value: str) -> str:
        return normalize_ref(value, expected_type=NodeType.COLUMN)


class JoinPathProperties(NodeModel):
    from_table_ref: str
    to_table_ref: str
    cardinality: JoinCardinality | None = JoinCardinality.UNKNOWN
    default_join_type: JoinType | None = JoinType.LEFT
    join_conditions: list[JoinCondition] = Field(default_factory=list)
    required_filters: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("from_table_ref", "to_table_ref")
    @classmethod
    def _validate_table_ref(cls, value: str) -> str:
        return normalize_ref(value, expected_type=NodeType.TABLE)

    _normalize_cardinality = field_validator("cardinality", mode="before")(_uppercase)
    _normalize_join_type = field_validator("default_join_type", mode="before")(_uppercase)
