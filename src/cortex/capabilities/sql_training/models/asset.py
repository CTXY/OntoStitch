from __future__ import annotations

import unicodedata
from typing import TypeAlias

from pydantic import Field, ValidationInfo, field_validator, model_validator

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import parse_node_id
from cortex.capabilities.sql_training.models.base import NodeModel, Source
from cortex.capabilities.sql_training.models.column import ColumnProperties
from cortex.capabilities.sql_training.models.dimension_value import (
    DimensionValueProperties,
)
from cortex.capabilities.sql_training.models.event import EventProperties
from cortex.capabilities.sql_training.models.example import ExampleProperties
from cortex.capabilities.sql_training.models.generation_rule import (
    GenerationRuleProperties,
)
from cortex.capabilities.sql_training.models.metric import MetricProperties
from cortex.capabilities.sql_training.models.semantic_mapping import (
    ConceptProperties,
)
from cortex.capabilities.sql_training.models.table import TableProperties

NodeProperties: TypeAlias = (
    TableProperties
    | ColumnProperties
    | DimensionValueProperties
    | MetricProperties
    | ConceptProperties
    | EventProperties
    | GenerationRuleProperties
    | ExampleProperties
)

_PROPERTIES_BY_TYPE: dict[NodeType, type[NodeProperties]] = {
    NodeType.TABLE: TableProperties,
    NodeType.COLUMN: ColumnProperties,
    NodeType.DIMENSION_VALUE: DimensionValueProperties,
    NodeType.METRIC: MetricProperties,
    NodeType.SEMANTIC_MAPPING: ConceptProperties,
    NodeType.EVENT: EventProperties,
    NodeType.GENERATION_RULE: GenerationRuleProperties,
    NodeType.EXAMPLE: ExampleProperties,
}


class NodeAsset(NodeModel):
    id: str
    node_type: NodeType
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    properties: NodeProperties
    sources: list[Source] = Field(default_factory=list)

    @field_validator("aliases", mode="before")
    @classmethod
    def _normalize_aliases(cls, value: object, info: ValidationInfo) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            # Pydantic must surface this as a field validation error.
            raise ValueError("aliases must be a list of strings")
        if info.data.get("node_type") == NodeType.DIMENSION_VALUE:
            # DB-verified SQL spellings preserve exact case and whitespace.
            return list(dict.fromkeys(value))
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = item.strip()
            key = unicodedata.normalize("NFKC", text).casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    @model_validator(mode="before")
    @classmethod
    def _select_properties_model(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        raw_type = data.get("node_type")
        raw_properties = data.get("properties")
        if raw_type is None or raw_properties is None:
            return data
        node_type = NodeType(raw_type)
        normalized = dict(data)
        normalized["properties"] = _PROPERTIES_BY_TYPE[node_type].model_validate(
            raw_properties
        )
        return normalized

    @model_validator(mode="after")
    def _validate_id_matches_type(self) -> NodeAsset:
        parsed = parse_node_id(self.id)
        if parsed.type != self.node_type:
            raise ValueError("Asset ID type must match asset type")
        if not self.name.strip():
            raise ValueError("Ontology Node name must be non-empty")
        if self.node_type == NodeType.EVENT and not (
            self.description and self.description.strip()
        ):
            raise ValueError("Event requires a business occurrence type description")
        if self.node_type == NodeType.DIMENSION_VALUE:
            self.aliases = [
                alias for alias in self.aliases if alias != str(self.properties.value)
            ]
            return self
        name_key = unicodedata.normalize("NFKC", self.name.strip()).casefold()
        self.aliases = [
            alias
            for alias in self.aliases
            if unicodedata.normalize("NFKC", alias).casefold() != name_key
        ]
        return self

    def references(self) -> list[str]:
        data = self.properties
        refs: list[str] = []
        if isinstance(
            data,
            (
                ColumnProperties,
                DimensionValueProperties,
                ExampleProperties,
            ),
        ):
            refs.extend(data.references)
        if isinstance(data, MetricProperties):
            refs.extend(data.expression.refs.values())
        if isinstance(data, (ConceptProperties, EventProperties)):
            for target in data.targets:
                refs.extend(target)
        return list(dict.fromkeys(refs))
