from __future__ import annotations

import json
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from cortex.schema.models import _SchemaModel


class SourceReference(_SchemaModel):
    source_id: str
    chunk_id: str | None = None
    locator: str | None = None
    case_id: str | None = None

    @field_validator("source_id")
    @classmethod
    def require_source_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source_id must not be empty")
        return value


class MergeStatus(StrEnum):
    NORMAL = "normal"
    CONFLICT = "conflict"


class GraphNode(_SchemaModel):
    id: str
    node_type: str
    name: str
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    sources: tuple[SourceReference, ...] = ()
    merge_status: MergeStatus = MergeStatus.NORMAL

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_metric_physical_dependencies(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("node_type") != "metric":
            return value
        properties = value.get("properties")
        if not isinstance(properties, dict) or "physical_dependencies" not in properties:
            return value
        if properties["physical_dependencies"] not in (None, []):
            raise ValueError("nonempty legacy metric physical_dependencies needs migration")
        normalized = dict(value)
        normalized["properties"] = {
            key: item for key, item in properties.items() if key != "physical_dependencies"
        }
        return normalized


class GraphEdge(_SchemaModel):
    id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    sources: tuple[SourceReference, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_mapping_operator(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("edge_type") not in {
            "maps_to_column",
            "maps_to_value",
        }:
            return value
        properties = value.get("properties")
        if not isinstance(properties, dict):
            return value
        operator = str(properties.get("binding_operator") or "").strip().casefold()
        normalized_operator = {"=": "exact", "or": "one_of", "and": "all_of"}.get(
            operator
        )
        if normalized_operator is None:
            return value
        normalized = dict(value)
        normalized["properties"] = {
            **properties,
            "binding_operator": normalized_operator,
        }
        return normalized


class GraphSpace(_SchemaModel):
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("GraphSpace contains duplicate node ids")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("GraphSpace contains duplicate edge ids")
        return self


class GraphFragment(_SchemaModel):
    node: GraphNode
    outgoing_edges: tuple[GraphEdge, ...] = ()
    relationship_compiler_version: str

    @model_validator(mode="after")
    def validate_outgoing_edges(self) -> Self:
        invalid = [
            edge.id for edge in self.outgoing_edges if edge.from_node_id != self.node.id
        ]
        if invalid:
            raise ValueError(f"GraphFragment contains non-outgoing edges: {invalid}")
        return self


class ConflictAlternative(_SchemaModel):
    alternative_id: str = Field(min_length=1)
    content: dict[str, Any]
    sources: tuple[SourceReference, ...] = ()


class ConflictChoice(_SchemaModel):
    """One coherent position for a specific unresolved difference."""

    alternative_ids: tuple[str, ...] = Field(min_length=1)
    source_case_ids: tuple[str, ...] = ()
    statement: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_field_paths(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "field_paths" not in value:
            return value
        normalized = dict(value)
        normalized.pop("field_paths", None)
        return normalized


class ConflictPoint(_SchemaModel):
    """One decision that remains unresolved across complete alternatives."""

    point_id: str = Field(min_length=1)
    aspect: str = Field(min_length=1)
    description: str = Field(min_length=1)
    choices: tuple[ConflictChoice, ...] = Field(min_length=2)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_impact(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "description" in value or "impact" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("impact")
        return normalized


class NodeMergeConflict(_SchemaModel):
    # None denotes a legacy container whose semantic status is unknown.
    consolidation_status: Literal["semantic_conflict", "unresolved"] | None = None
    preservation_reasons: tuple[str, ...] = ()
    consolidation: dict[str, Any] = Field(default_factory=dict)
    node_id: str = Field(min_length=1)
    alternatives: tuple[ConflictAlternative, ...] = Field(min_length=2)
    conflict_points: tuple[ConflictPoint, ...] = ()

    @model_validator(mode="after")
    def validate_alternatives(self) -> Self:
        ids = [alternative.alternative_id for alternative in self.alternatives]
        if len(ids) != len(set(ids)):
            raise ValueError("conflict alternative ids must be unique")
        contents = [
            json.dumps(
                alternative.content, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            )
            for alternative in self.alternatives
        ]
        if len(contents) != len(set(contents)):
            raise ValueError("conflict alternative contents must be distinct")
        alternative_ids = set(ids)
        point_ids = [point.point_id for point in self.conflict_points]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("conflict point ids must be unique")
        for point in self.conflict_points:
            referenced = {
                alternative_id
                for choice in point.choices
                for alternative_id in choice.alternative_ids
            }
            if not referenced <= alternative_ids:
                raise ValueError("conflict choices must reference existing alternatives")
            if len(referenced) < 2:
                raise ValueError("a conflict point must distinguish at least two alternatives")
        return self


class GraphState(_SchemaModel):
    graph: GraphSpace
    node_conflicts: tuple[NodeMergeConflict, ...]
