from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cortex.application.online_context import CandidateEvidenceAssessment, ChoicePoint, SlotScope
from cortex.schema import GraphState


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateDraft(_Model):
    candidate_id: str
    statement: str
    sources: tuple[Literal["memory_graph", "initial_sql"], ...] = ()
    graph_refs: tuple[str, ...] = Field(
        default=(),
        description=(
            "Exact supplied handles supporting this Candidate. For expressions such as "
            "COUNT(*) that have no direct Column handle, cite the supporting rule, "
            "alternative, or parent signal handle."
        ),
    )
    initial_sql_evidence: str | None = None
    evidence_assessment: CandidateEvidenceAssessment


class SlotDraft(_Model):
    slot_id: str
    scope: SlotScope
    decision_question: str
    candidates: tuple[CandidateDraft, ...] = Field(min_length=2)


class IntraNodeAssessment(_Model):
    signal_node_id: str
    status: Literal["confirmed", "not_relevant", "not_conflicting", "uncertain"]
    slot_ids: tuple[str, ...] = ()
    reason: str


class IntraNodeOutput(_Model):
    assessments: tuple[IntraNodeAssessment, ...]
    slots: tuple[SlotDraft, ...]


class ProvenanceAddition(_Model):
    candidate_id: str
    graph_refs: tuple[str, ...] = ()
    initial_sql_evidence: str | None = None
    evidence_assessment: CandidateEvidenceAssessment


class SlotAugmentation(_Model):
    slot_id: str
    provenance_additions: tuple[ProvenanceAddition, ...] = ()
    new_candidates: tuple[CandidateDraft, ...] = ()


class InitialSqlMemoryOutput(_Model):
    slot_augmentations: tuple[SlotAugmentation, ...]
    new_slots: tuple[SlotDraft, ...]


class CrossNodeOutput(_Model):
    slot_augmentations: tuple[SlotAugmentation, ...]
    new_slots: tuple[SlotDraft, ...]


class SlotRelevanceDecision(_Model):
    slot_id: str
    keep: bool
    reason: str


class SlotRelevanceOutput(_Model):
    decisions: tuple[SlotRelevanceDecision, ...]


class ChoiceOrganizationResult(_Model):
    choice_points: tuple[ChoicePoint, ...]
    intra_node_assessments: tuple[IntraNodeAssessment, ...] = ()


class ChoiceOrganizer(Protocol):
    async def organize(
        self,
        *,
        question: str,
        initial_sql: str | None,
        retrieved_graph_state: GraphState,
    ) -> ChoiceOrganizationResult: ...
