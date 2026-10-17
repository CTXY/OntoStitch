from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator

from cortex.capabilities.sql_training.core.ids import normalize_ref
from cortex.capabilities.sql_training.models.base import NodeModel


class MapsToColumn(NodeModel):
    relation: Literal["maps_to_column"]
    node_id: str

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return normalize_ref(value, expected_type="column")


class MapsToValue(NodeModel):
    relation: Literal["maps_to_value"]
    node_id: str

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        return normalize_ref(value, expected_type="dimension_value")


ConceptSemanticTarget = Annotated[
    MapsToColumn | MapsToValue,
    Field(discriminator="relation"),
]
