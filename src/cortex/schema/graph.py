from __future__ import annotations

import copy
import json
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_serializer, model_validator

from cortex.schema.models import EdgeType, _SchemaModel


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


class NodeContent(_SchemaModel):
    name: str
    description: str | None
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    properties: dict[str, Any]

    @field_validator("name")
    @classmethod
    def require_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate name must be nonempty")
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
    consolidation_status: Literal["semantic_conflict", "unresolved"] | None = None
    preservation_reasons: tuple[str, ...] = ()
    alternatives: tuple[ConflictAlternative, ...] = ()
    conflict_points: tuple[ConflictPoint, ...] = ()

    @model_validator(mode="after")
    def validate_normal_metadata(self) -> Self:
        MergeStatus(self.merge_status)
        if self.merge_status == MergeStatus.NORMAL and (
            self.consolidation_status is not None
            or self.preservation_reasons
            or self.alternatives
            or self.conflict_points
        ):
            raise ValueError("normal nodes cannot contain conflict metadata")
        return self

    def model_copy(
        self, *, update: dict[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update and "merge_status" in update:
            update = {**update, "merge_status": MergeStatus(update["merge_status"])}
        copied = super().model_copy(update=update, deep=deep)
        copied.validate_normal_metadata()
        return copied

    @model_serializer(mode="wrap")
    def serialize_node(self, handler):
        self.validate_normal_metadata()
        payload = handler(self)
        if self.merge_status != MergeStatus.CONFLICT:
            for field in (
                "consolidation_status",
                "preservation_reasons",
                "alternatives",
                "conflict_points",
            ):
                payload.pop(field, None)
        return payload

    def with_conflict(self, proposal: NodeMergeConflict) -> GraphNode:
        """Attach proposal metadata to its owner; no sidecar is retained."""
        if proposal.node_id != self.id:
            raise ValueError("conflict proposal owner differs from node id")
        return self.model_copy(
            update={
                "merge_status": MergeStatus.CONFLICT,
                "consolidation_status": proposal.consolidation_status,
                "preservation_reasons": proposal.preservation_reasons,
                "alternatives": proposal.alternatives,
                "conflict_points": proposal.conflict_points,
            }
        )

    def without_conflict(self) -> GraphNode:
        """Clear conflict metadata when materializing one complete definition."""
        return self.model_copy(
            update={
                "merge_status": MergeStatus.NORMAL,
                "consolidation_status": None,
                "preservation_reasons": (),
                "alternatives": (),
                "conflict_points": (),
            }
        )

    def as_conflict(self) -> NodeMergeConflict:
        """Derive a legacy/proposal DTO at an explicit compatibility boundary."""
        if self.merge_status != MergeStatus.CONFLICT:
            raise ValueError("normal node has no conflict metadata")
        return NodeMergeConflict(
            node_id=self.id,
            consolidation_status=self.consolidation_status or "unresolved",
            preservation_reasons=self.preservation_reasons,
            alternatives=self.alternatives,
            conflict_points=self.conflict_points,
        )


class GraphEdge(_SchemaModel):
    id: str
    edge_type: str
    from_node_id: str
    to_node_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    sources: tuple[SourceReference, ...] = ()
    reference_origin: Literal["default", "explicit"] | None = None


class GraphSpace(_SchemaModel):
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    relation_contracts: tuple[EdgeType, ...] = Field(
        default=(), exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("GraphSpace contains duplicate node ids")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("GraphSpace contains duplicate edge ids")
        contract_ids = [contract.id for contract in self.relation_contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("GraphSpace contains duplicate relation contracts")
        return self


class ConflictAlternative(_SchemaModel):
    alternative_id: str = Field(min_length=1)
    overrides: dict[str, Any]
    sources: tuple[SourceReference, ...] = ()


class ConflictChoice(_SchemaModel):
    """One coherent position for a specific unresolved difference."""

    alternative_ids: tuple[str, ...] = Field(min_length=1)
    statement: str = Field(min_length=1)


class ConflictPoint(_SchemaModel):
    """One decision that remains unresolved across complete alternatives."""

    point_id: str = Field(min_length=1)
    aspect: str = Field(min_length=1)
    description: str = Field(min_length=1)
    choices: tuple[ConflictChoice, ...] = Field(min_length=2)


class NodeMergeConflict(_SchemaModel):
    consolidation_status: Literal["semantic_conflict", "unresolved"] = "unresolved"
    preservation_reasons: tuple[str, ...] = ()
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
                alternative.overrides,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for alternative in self.alternatives
        ]
        if len(contents) != len(set(contents)):
            raise ValueError("conflict alternative overrides must be distinct")
        paths = set(self.alternatives[0].overrides)
        if not paths or any(set(a.overrides) != paths for a in self.alternatives):
            raise ValueError(
                "alternatives must override the same nonempty set of fields"
            )
        for path in paths:
            if path not in {"name", "description", "aliases", "tags"} and not (
                path.startswith("properties.")
                and len(path.split(".")) == 2
                and path.split(".")[1]
            ):
                raise ValueError(
                    f"unsupported alternative field {path!r}; replace a whole declared field"
                )
            values = [
                json.dumps(a.overrides[path], sort_keys=True, allow_nan=False)
                for a in self.alternatives
            ]
            if len(set(values)) == 1:
                raise ValueError(f"shared field {path!r} belongs on the common node")
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
                raise ValueError(
                    "conflict choices must reference existing alternatives"
                )
            if len(referenced) < 2:
                raise ValueError(
                    "a conflict point must distinguish at least two alternatives"
                )
        return self


GraphNode.model_rebuild()


class GraphState(_SchemaModel):
    graph: GraphSpace

    @model_validator(mode="before")
    @classmethod
    def import_inline_conflicts(cls, value: Any) -> Any:
        from cortex.schema.graph_io import import_inline_graph

        return import_inline_graph(value)

    @property
    def conflict_nodes(self) -> tuple[GraphNode, ...]:
        """The actual conflicted graph nodes, never a second stored collection."""
        return tuple(
            node
            for node in self.graph.nodes
            if node.merge_status == MergeStatus.CONFLICT
        )

    @property
    def node_conflicts(self) -> tuple[NodeMergeConflict, ...]:
        """Deprecated legacy read view; use conflict_nodes in production code."""
        return tuple(node.as_conflict() for node in self.conflict_nodes)

    def model_copy(
        self, *, update: dict[str, Any] | None = None, deep: bool = False
    ) -> Self:
        if update and {"node_conflicts", "conflict_nodes"}.intersection(update):
            raise ValueError(
                "node_conflicts is import-only; update inline graph nodes or use with_conflicts"
            )
        return super().model_copy(update=update, deep=deep)


def alternative_content(
    node: GraphNode, alternative: ConflictAlternative
) -> NodeContent:
    """Restore whole fields only; null removes an optional property."""
    content = copy.deepcopy(
        {key: getattr(node, key) for key in NodeContent.model_fields}
    )
    for path, value in alternative.overrides.items():
        if path.startswith("properties.") and len(path.split(".")) == 2:
            key = path.split(".")[1]
            if value is None:
                content["properties"].pop(key, None)
            else:
                content["properties"][key] = copy.deepcopy(value)
        elif path in {"name", "description", "aliases", "tags"}:
            content[path] = copy.deepcopy(value)
        else:
            raise ValueError(f"unsupported alternative field {path!r}")
    return NodeContent.model_validate(content)
