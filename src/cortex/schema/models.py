from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELD_TYPE_RE = re.compile(
    r"^(?:string|number|integer|boolean|date|datetime|object|array)(?:\[\])?"
    r"(?:\|(?:string|number|integer|boolean|date|datetime|object|array)(?:\[\])?)*$"
)

EnumValue = str | int | float | bool


class _SchemaModel(BaseModel):
    """Strict immutable base for shared Schema and graph data definitions."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaFamily(StrEnum):
    STRUCTURED = "structured"
    UNSTRUCTURED = "unstructured"
    COMMON = "common"


def validate_identifier(value: str, *, label: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must use lowercase letters, numbers, and underscores: {value!r}")
    return value


class FieldDefinition(_SchemaModel):
    type: str
    required: bool = False
    description: str | None = None
    enum: list[EnumValue] | None = Field(default=None, min_length=1)
    default: Any = None

    @field_validator("type")
    @classmethod
    def validate_type_expression(cls, value: str) -> str:
        if not _FIELD_TYPE_RE.fullmatch(value):
            raise ValueError(f"unsupported field type expression: {value!r}")
        tokens = value.split("|")
        if len(tokens) != len(set(tokens)):
            raise ValueError(f"field type expression contains duplicates: {value!r}")
        return value


class IdentityMode(StrEnum):
    EXACT = "exact"
    SEMANTIC = "semantic"


class MergeRole(StrEnum):
    FACT = "fact"
    KNOWLEDGE = "knowledge"


class EdgeDirectionality(StrEnum):
    DIRECTED = "directed"
    UNDIRECTED = "undirected"


class IdentityPolicy(_SchemaModel):
    mode: IdentityMode
    fields: tuple[str, ...] = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def validate_identity_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("identity fields cannot contain duplicates")
        for path in value:
            if path in {"name", "description", "aliases", "tags"}:
                continue
            if path.startswith("properties.") and path.count(".") == 1:
                validate_identifier(path.removeprefix("properties."), label="identity field")
                continue
            raise ValueError(f"unsupported identity field path: {path!r}")
        return value


class NodeType(_SchemaModel):
    id: str
    family: SchemaFamily
    name: str
    description: str
    extends: str | None = None
    fields: dict[str, FieldDefinition] = Field(default_factory=dict)
    inclusion_criteria: list[str] = Field(default_factory=list)
    exclusion_criteria: list[str] = Field(default_factory=list)
    merge_role: MergeRole = MergeRole.FACT
    identity_policy: IdentityPolicy

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value, label="NodeType.id")

    @field_validator("extends")
    @classmethod
    def validate_extends(cls, value: str | None) -> str | None:
        return validate_identifier(value, label="NodeType.extends") if value else None

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, FieldDefinition]) -> dict[str, FieldDefinition]:
        for field_id in value:
            validate_identifier(field_id, label="NodeType field")
        return value


class EdgeType(_SchemaModel):
    id: str
    name: str
    description: str
    from_node_types: list[str] = Field(min_length=1)
    to_node_types: list[str] = Field(min_length=1)
    fields: dict[str, FieldDefinition] = Field(default_factory=dict)
    direction_semantics: str | None = None
    evidence_requirement: str | None = None
    directionality: EdgeDirectionality = Field(
        default=EdgeDirectionality.DIRECTED,
        exclude_if=lambda value: value == EdgeDirectionality.DIRECTED,
    )
    identity_fields: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_identifier(value, label="EdgeType.id")

    @field_validator("from_node_types", "to_node_types")
    @classmethod
    def validate_endpoints(cls, value: list[str]) -> list[str]:
        for node_type_id in value:
            validate_identifier(node_type_id, label="EdgeType endpoint")
        if len(value) != len(set(value)):
            raise ValueError("EdgeType endpoint lists cannot contain duplicates")
        return value

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value: dict[str, FieldDefinition]) -> dict[str, FieldDefinition]:
        for field_id in value:
            validate_identifier(field_id, label="EdgeType field")
        return value


class SchemaDefinition(_SchemaModel):
    node_types: tuple[NodeType, ...]
    edge_types: tuple[EdgeType, ...]

    @model_validator(mode="after")
    def validate_definition(self) -> Self:
        node_types = self.node_type_index()
        edge_types = self.edge_type_index()
        self._validate_inheritance(node_types)
        for node_type in node_types.values():
            effective_fields = self.effective_node_fields(node_type.id)
            for path in node_type.identity_policy.fields:
                if path.startswith("properties."):
                    field_id = path.removeprefix("properties.")
                    if field_id not in effective_fields:
                        raise ValueError(
                            f"NodeType {node_type.id!r} identity references unknown "
                            f"field {field_id!r}"
                        )
        for edge_type in edge_types.values():
            for endpoint in (*edge_type.from_node_types, *edge_type.to_node_types):
                if endpoint not in node_types:
                    raise ValueError(
                        f"EdgeType {edge_type.id!r} references unknown NodeType {endpoint!r}"
                    )
            if len(edge_type.identity_fields) != len(set(edge_type.identity_fields)):
                raise ValueError(
                    f"EdgeType {edge_type.id!r} identity fields cannot contain duplicates"
                )
            for field_id in edge_type.identity_fields:
                field = edge_type.fields.get(field_id)
                if field is None:
                    raise ValueError(
                        f"EdgeType {edge_type.id!r} identity references unknown field {field_id!r}"
                    )
                if not field.required:
                    raise ValueError(
                        f"EdgeType {edge_type.id!r} identity field {field_id!r} must be required"
                    )
        return self

    def node_type_index(self) -> dict[str, NodeType]:
        result = {node_type.id: node_type for node_type in self.node_types}
        if len(result) != len(self.node_types):
            raise ValueError("duplicate NodeType id")
        return result

    def edge_type_index(self) -> dict[str, EdgeType]:
        result = {edge_type.id: edge_type for edge_type in self.edge_types}
        if len(result) != len(self.edge_types):
            raise ValueError("duplicate EdgeType id")
        return result

    def is_node_type_compatible(self, actual_type_id: str, expected_type_id: str) -> bool:
        node_types = self.node_type_index()
        current = node_types[actual_type_id]
        while True:
            if current.id == expected_type_id:
                return True
            if current.extends is None:
                return False
            current = node_types[current.extends]

    def effective_node_fields(self, type_id: str) -> dict[str, FieldDefinition]:
        """Return inherited fields from the root type through the concrete type."""

        node_types = self.node_type_index()
        lineage: list[NodeType] = []
        current = node_types[type_id]
        while True:
            lineage.append(current)
            if current.extends is None:
                break
            current = node_types[current.extends]
        fields: dict[str, FieldDefinition] = {}
        for node_type in reversed(lineage):
            fields.update(node_type.fields)
        return fields

    @staticmethod
    def _validate_inheritance(node_types: dict[str, NodeType]) -> None:
        for node_type in node_types.values():
            seen: set[str] = set()
            current = node_type
            while current.extends is not None:
                if current.extends not in node_types:
                    raise ValueError(
                        f"NodeType {current.id!r} extends unknown NodeType {current.extends!r}"
                    )
                if current.id in seen:
                    raise ValueError(f"NodeType inheritance cycle includes {current.id!r}")
                seen.add(current.id)
                parent = node_types[current.extends]
                if parent.family != current.family:
                    raise ValueError("NodeType inheritance cannot cross schema families")
                for field_id, field in current.fields.items():
                    inherited = parent.fields.get(field_id)
                    if inherited and (
                        inherited.type != field.type or (inherited.required and not field.required)
                    ):
                        raise ValueError(
                            f"NodeType {current.id!r} incompatibly overrides field {field_id!r}"
                        )
                current = parent
