from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex.schema import GraphEdge, GraphNode, NodeMergeConflict, SourceReference
from cortex.schema.graph import NodeContent


class _GraphMergeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepresentationRef(_GraphMergeModel):
    """Stable pointer to one input representation used as group membership evidence."""

    origin: Literal["existing_node", "incoming_node", "existing_alternative"]
    ref_id: str


class NodeRepresentation(_GraphMergeModel):
    ref: RepresentationRef
    node_type: str
    content: NodeContent
    sources: tuple[SourceReference, ...]


class IdentityRepresentationView(_GraphMergeModel):
    ref: RepresentationRef
    sources: tuple[SourceReference, ...]
    identity_fields: dict[str, Any]
    key_implementation: dict[str, Any]


class GroupIdentityView(_GraphMergeModel):
    entry_id: str
    existing_node_id: str | None
    representations: tuple[IdentityRepresentationView, ...]

    @property
    def member_refs(self) -> tuple[RepresentationRef, ...]:
        return tuple(item.ref for item in self.representations)

    @property
    def identity_representations(self) -> tuple[dict[str, Any], ...]:
        return tuple(item.identity_fields for item in self.representations)


class NodeIdentityView(_GraphMergeModel):
    query_id: str
    node_type: str
    representation: IdentityRepresentationView

    @property
    def representation_ref(self) -> RepresentationRef:
        return self.representation.ref

    @property
    def identity_fields(self) -> dict[str, Any]:
        return self.representation.identity_fields


class FinalNodeGroup(_GraphMergeModel):
    group_id: str
    node_type: str
    existing_node_id: str | None
    members: tuple[NodeRepresentation, ...]


class QueryIdentityVerdict(_GraphMergeModel):
    query_id: str
    verdict: Literal["same", "different"]
    reason: str


class NodeIdentitySelection(_GraphMergeModel):
    selected_entry_id: str | None
    query_verdicts: tuple[QueryIdentityVerdict, ...]
    reason: str

    def validate_candidates(self, entry_ids: set[str], query_ids: set[str]) -> None:
        if (
            self.selected_entry_id is not None
            and self.selected_entry_id not in entry_ids
        ):
            raise ValueError(
                f"selected_entry_id must be null or one of {sorted(entry_ids)}"
            )
        returned = [item.query_id for item in self.query_verdicts]
        if len(returned) != len(set(returned)) or set(returned) != query_ids:
            raise ValueError(
                f"query_verdicts must cover each of {sorted(query_ids)} exactly once"
            )


class IdentityGroupingResult(_GraphMergeModel):
    groups: tuple[FinalNodeGroup, ...]
    cross_type_replacements: tuple[
        tuple[RepresentationRef, RepresentationRef], ...
    ] = ()
    diagnostic_codes: tuple[str, ...] = ()


class CrossTypeCandidate(_GraphMergeModel):
    pair_id: str
    concept: NodeRepresentation
    metric: NodeRepresentation


class CrossTypeDecision(_GraphMergeModel):
    pair_id: str
    verdict: Literal["same_object", "different"]
    keep_ref: RepresentationRef | None = None
    reason: str


class CrossTypeResolution(_GraphMergeModel):
    decisions: tuple[CrossTypeDecision, ...]


class EdgeNormalizationResult(_GraphMergeModel):
    edges: tuple[GraphEdge, ...]
    mappings: tuple[tuple[str, str], ...]
    variant_edge_ids: tuple[str, ...] = ()
    invalidated_edge_ids: tuple[str, ...] = ()


class EvidenceItem(_GraphMergeModel):
    id: str
    source: SourceReference
    case_id: str | None
    content: dict[str, Any]


class ReconciledRepresentation(_GraphMergeModel):
    """One constructed or preserved implementation, with its complete member provenance."""

    content: NodeContent
    supporting_refs: tuple[RepresentationRef, ...]


class DifferenceMember(_GraphMergeModel):
    """An input representation and the source cases supporting its position."""

    representation_ref: RepresentationRef
    source_case_ids: tuple[str, ...]


class DifferencePosition(_GraphMergeModel):
    """One complete position observed at a semantic difference point."""

    members: tuple[DifferenceMember, ...] = Field(min_length=1)
    statement: str = Field(min_length=1)


class DifferencePoint(_GraphMergeModel):
    """A published unresolved choice between complete competing definitions."""

    point_id: str = Field(min_length=1)
    aspect: Literal[
        "concept_description",
        "concept_target",
        "metric_description",
        "metric_expression",
        "metric_plan",
        "rule_condition",
        "rule_action",
        "references",
        "other",
    ]
    positions: tuple[DifferencePosition, ...] = Field(min_length=2)
    resolution: Literal["conflict"]
    reason: str = Field(min_length=1)


class ReconciledNode(_GraphMergeModel):
    consolidation: dict[str, Any] = Field(default_factory=dict)
    node: GraphNode
    conflict: NodeMergeConflict | None = None
    representations: tuple[NodeRepresentation, ...]
    evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]]
    diagnostic_codes: tuple[str, ...] = ()
