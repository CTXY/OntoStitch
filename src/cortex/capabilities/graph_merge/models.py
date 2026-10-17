from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cortex.schema import GraphEdge, SourceReference


class _GraphMergeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeContent(_GraphMergeModel):
    name: str
    description: str | None
    aliases: tuple[str, ...]
    tags: tuple[str, ...]
    properties: dict[str, Any]


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
        if self.selected_entry_id is not None and self.selected_entry_id not in entry_ids:
            raise ValueError(f"selected_entry_id must be null or one of {sorted(entry_ids)}")
        returned = [item.query_id for item in self.query_verdicts]
        if len(returned) != len(set(returned)) or set(returned) != query_ids:
            raise ValueError(f"query_verdicts must cover each of {sorted(query_ids)} exactly once")


class IdentityGroupingResult(_GraphMergeModel):
    groups: tuple[FinalNodeGroup, ...]
    cross_type_replacements: tuple[tuple[RepresentationRef, RepresentationRef], ...] = ()
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


class BranchReconciliationGroup(_GraphMergeModel):
    node_id: str
    alternative_id: str
    root: NodeRepresentation
    incoming_members: tuple[NodeRepresentation, ...]


class ReconciledRepresentation(_GraphMergeModel):
    """One constructed or preserved implementation, with its complete member provenance."""

    target_alternative_id: str | None = None
    content: NodeContent
    supporting_refs: tuple[RepresentationRef, ...]


class SharedSemantic(_GraphMergeModel):
    """A semantic statement supported by every representation in a group."""

    semantic_point: str
    statement: str
    supporting_refs: tuple[RepresentationRef, ...]


class SemanticResidual(_GraphMergeModel):
    """Source-relative behavior left after removing the proposed shared semantics."""

    residual_id: str
    semantic_point: str
    statement: str
    applies_when: str | None
    supporting_refs: tuple[RepresentationRef, ...]


class ResidualRelation(_GraphMergeModel):
    """Whether residuals can coexist in one behavior-preserving generalization."""

    residual_refs: tuple[str, ...]
    relation: Literal["compatible", "conditional", "refinement", "conflict"]
    condition: str | None = None
    reason: str


class DifferenceMember(_GraphMergeModel):
    """An input representation and the source cases supporting its position."""

    representation_ref: RepresentationRef
    source_case_ids: tuple[str, ...]


class DifferencePosition(_GraphMergeModel):
    """One complete position observed at a semantic difference point."""

    members: tuple[DifferenceMember, ...] = Field(min_length=1)
    statement: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_field_paths(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "field_paths" not in value:
            return value
        normalized = dict(value)
        normalized.pop("field_paths", None)
        return normalized


class DifferencePoint(_GraphMergeModel):
    """A field-level semantic difference and its reconciliation disposition."""

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
    # Accept a model's structurally redundant one-position observation so the
    # reconciliation boundary can discard it without losing the rest of an
    # otherwise valid analysis. A published difference still requires two
    # positions; that invariant is enforced after normalization.
    positions: tuple[DifferencePosition, ...] = Field(min_length=1)
    resolution: Literal["equivalent", "complementary", "generalized", "conditional", "conflict"]
    reason: str = Field(min_length=1)


class ReconciliationGroup(_GraphMergeModel):
    supporting_refs: tuple[RepresentationRef, ...]
    baseline_ref: RepresentationRef
    relation: Literal["equivalent", "compatible"]
    target_alternative_id: str | None = None
    reason_code: str
    reason: str
    shared: tuple[SharedSemantic, ...] = ()
    residuals: tuple[SemanticResidual, ...] = ()
    residual_relations: tuple[ResidualRelation, ...] = ()


class ReconciliationNodeObservation(_GraphMergeModel):
    representation_ref: RepresentationRef
    requested_result: str = Field(min_length=1)
    action: str = Field(min_length=1)
    exact_operands: tuple[str, ...] = ()


class DifferenceExplanation(_GraphMergeModel):
    difference_id: str
    supporting_refs: tuple[RepresentationRef, ...]
    observed_difference: str
    explanation: str
    applies_when: str | None = None
    evidence_ids: tuple[str, ...] = ()
    uncertainty: str | None = None


class ReconcileOutcome(_GraphMergeModel):
    observations: tuple[ReconciliationNodeObservation, ...] = ()
    explanations: tuple[DifferenceExplanation, ...] = ()
    groups: tuple[ReconciliationGroup, ...]
    difference_points: tuple[DifferencePoint, ...] = ()
    reason: str
    unresolved_refs: tuple[RepresentationRef, ...] = ()


class ConsolidationCandidate(_GraphMergeModel):
    content: NodeContent
    supporting_refs: tuple[RepresentationRef, ...] = Field(min_length=1)


class ConsolidationDifferencePoint(DifferencePoint):
    """Published competition only; legacy analysis can still use broader DifferencePoint."""

    resolution: Literal["conflict"]
    positions: tuple[DifferencePosition, ...] = Field(min_length=2)


class ConsolidationProposal(_GraphMergeModel):
    candidates: tuple[ConsolidationCandidate, ...]
    difference_points: tuple[ConsolidationDifferencePoint, ...] = ()
    unresolved_refs: tuple[RepresentationRef, ...] = ()
    reason: str

    @field_validator("difference_points", mode="before")
    @classmethod
    def validate_existing_points(cls, points):
        if not isinstance(points, (list, tuple)):
            return points
        return tuple(
            p.model_dump(mode="python") if isinstance(p, DifferencePoint) else p for p in points
        )


class ReconciliationPairDecision(_GraphMergeModel):
    left_ref: RepresentationRef
    right_ref: RepresentationRef
    left_action: str = Field(min_length=1)
    right_action: str = Field(min_length=1)
    context_relation: Literal["same", "overlap", "parallel", "disjoint", "general_and_concrete"]
    action_relation: Literal["equivalent", "different"]
    selector_status: Literal["proved", "none"]
    selector: str | None = None
    resolution: Literal[
        "equivalent", "generalize", "conditional", "refinement", "disjoint", "conflict"
    ]
    witness: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class RequestedResultSlot(_GraphMergeModel):
    member_refs: tuple[RepresentationRef, ...] = Field(min_length=1)
    requested_result: str = Field(min_length=1)


class RequestedResultPartition(_GraphMergeModel):
    slots: tuple[RequestedResultSlot, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReconciliationInputAnalysis(_GraphMergeModel):
    nodes: tuple[ReconciliationNodeObservation, ...] = Field(min_length=1)
    requested_results: RequestedResultPartition


class BranchSelection(_GraphMergeModel):
    selected_alternative_id: str | None
    reason: str


class SemanticFieldProvenance(_GraphMergeModel):
    supporting_ref: RepresentationRef
    source_path: str
    output_path: str | None
    transformation: Literal["exact", "parameterized", "semantic", "omitted"]
    reason: str


class GeneralizationProposal(_GraphMergeModel):
    action: Literal["generalize", "unable"]
    content: NodeContent | None
    reason: str
    field_provenance: tuple[SemanticFieldProvenance, ...] = ()


class FusionProposal(_GraphMergeModel):
    action: Literal["fuse", "unable"]
    content: NodeContent | None
    reason: str


FeedbackKind = Literal[
    "unnecessary_specialization",
    "candidate_defect",
    "generation_failure",
    "unsupported_condition",
    "dependency_misinterpretation",
    "unresolved_competition",
    "insufficient_evidence",
    "contract_error",
    "model_failure",
]


class SourceCoverageVerdict(_GraphMergeModel):
    supporting_ref: RepresentationRef
    evidence_id: str
    verdict: Literal["covered", "not_covered", "unknown"]
    condition_verdict: Literal["covered", "not_covered", "unknown"]
    feedback_kind: FeedbackKind | None = None
    reason: str


class CoverageVerdict(_GraphMergeModel):
    supporting_ref: RepresentationRef
    verdict: Literal["covered", "not_covered", "unknown"]
    fields: tuple[str, ...] = ()
    counterevidence_ids: tuple[str, ...] = ()
    reason: str


class PlanCoverageVerdict(_GraphMergeModel):
    supporting_ref: RepresentationRef
    source_plan_id: str
    canonical_plan_id: str | None
    relation: Literal["exact", "semantic_equivalent", "complementary", "incompatible"]
    verdict: Literal["covered", "not_covered", "unknown"]
    counterevidence_ids: tuple[str, ...] = ()
    reason: str


class GeneralizationVerification(_GraphMergeModel):
    verdicts: tuple[CoverageVerdict, ...]
    source_verdicts: tuple[SourceCoverageVerdict, ...] = ()
    plan_verdicts: tuple[PlanCoverageVerdict, ...] = ()
    reason: str


class ReviewJudgment(_GraphMergeModel):
    verdict: Literal["acceptable", "revise", "unknown", "not_applicable"]
    reason: str


class ConsolidationReview(GeneralizationVerification):
    generalization: ReviewJudgment
    consistency: ReviewJudgment


class ReconciledNode(_GraphMergeModel):
    consolidation: dict[str, Any] = Field(default_factory=dict)
    node: Any
    conflict: Any | None = None
    representations: tuple[NodeRepresentation, ...]
    evidence_by_ref: Mapping[RepresentationRef, tuple[EvidenceItem, ...]]
    diagnostic_codes: tuple[str, ...] = ()
