from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cortex.application.online_context import (
    CandidateEvidenceAssessment,
    ChoicePoint,
    SlotScope,
)
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
            "Only supplied graph handles; never Q1/E1 user evidence IDs. Explain user evidence in statement/assessment. For expressions such as "
            "COUNT(*) that have no direct Column handle, cite the supporting rule, "
            "alternative, or parent signal handle."
        ),
    )
    initial_sql_evidence: str | None = Field(default=None, description="The actual initial SQL behavior, never hypothetical alternatives; at most one candidate per slot may cite initial SQL.")
    evidence_assessment: CandidateEvidenceAssessment


class SlotDraft(_Model):
    slot_id: str
    scope: SlotScope
    decision_question: str
    candidates: tuple[CandidateDraft, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def one_initial_sql_candidate(self):
        from .contracts import validate_initial_sql_sources
        validate_initial_sql_sources((self,))
        return self


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
    initial_sql_evidence: str | None = Field(default=None, description="The actual initial SQL behavior, never hypothetical alternatives; at most one candidate per slot may cite initial SQL.")
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
