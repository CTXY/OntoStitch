"""The single proposal/assessment contract used by the merge workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from cortex.capabilities.graph_merge.models import (
    DifferencePoint,
    NodeContent,
    RepresentationRef,
    _GraphMergeModel,
)


class DifferenceExplanation(_GraphMergeModel):
    difference: str
    explanation: str
    supporting_refs: tuple[RepresentationRef, ...]
    evidence_ids: tuple[str, ...] = ()
    applies_when: str | None = None


class ConsolidationAnalysis(_GraphMergeModel):
    shared: tuple[str, ...] = ()
    differences: tuple[DifferenceExplanation, ...] = ()


class ConsolidationCandidate(_GraphMergeModel):
    content: NodeContent
    supporting_refs: tuple[RepresentationRef, ...] = Field(min_length=1)


class ConsolidationProposal(_GraphMergeModel):
    analysis: ConsolidationAnalysis
    candidates: tuple[ConsolidationCandidate, ...]
    difference_points: tuple[DifferencePoint, ...] = ()
    unresolved_refs: tuple[RepresentationRef, ...] = ()
    identity_mismatch: str | None = Field(default=None, min_length=1)


class GeneralizationJudgment(_GraphMergeModel):
    verdict: Literal["acceptable", "revise", "unknown"]
    reason: str = Field(min_length=1)


class SourceCoverage(_GraphMergeModel):
    supporting_ref: RepresentationRef
    evidence_id: str
    verdict: Literal["covered", "not_covered", "unknown"]
    reason: str = Field(min_length=1)


class ConsolidationReview(_GraphMergeModel):
    generalization: GeneralizationJudgment
    coverage_by_source: tuple[SourceCoverage, ...]
    approved_relation_scope_changes: tuple[str, ...] = ()


def preserved_proposal(group) -> ConsolidationProposal:
    return ConsolidationProposal(
        analysis=ConsolidationAnalysis(),
        candidates=(),
        unresolved_refs=tuple(member.ref for member in group.members),
    )
