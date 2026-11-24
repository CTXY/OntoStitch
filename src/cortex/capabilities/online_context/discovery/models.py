from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator, ValidationInfo

from cortex.application.online_context import (
    CandidateEvidenceAssessment,
    ChoicePoint,
    SlotScope,
)
from cortex.schema import GraphState


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CitedEvidence(_Model):
    model_config = ConfigDict(json_schema_extra={
        "anyOf": [
            {"required": ["graph_refs"], "properties": {"graph_refs": {"minItems": 1}}},
            {"required": ["initial_sql_evidence"], "properties": {
                "initial_sql_evidence": {"type": "string", "minLength": 1},
            }},
        ],
    })

    candidate_id: str
    graph_refs: tuple[str, ...] = Field(
        default=(),
        description=(
            "Required and nonempty when source_types includes ontology_asset or database_schema. "
            "Only supplied graph handles; never Q1/E1 user evidence IDs. Explain user evidence in statement/assessment. For expressions such as "
            "COUNT(*) that have no direct Column handle, cite the supporting rule, "
            "alternative, or parent signal handle."
        ),
    )
    initial_sql_evidence: str | None = Field(default=None, description="Required and nonempty when source_types includes initial_sql: a verbatim fragment of the actual Initial SQL, never a hypothetical modification. At most one candidate per slot may cite Initial SQL. Use null when there is no SQL evidence; both evidence fields cannot be empty.")
    evidence_assessment: CandidateEvidenceAssessment

    initial_sql_refs: tuple[str, ...] = Field(default=(), exclude=True)

    @model_validator(mode="before")
    @classmethod
    def resolve_sql_refs(cls, value, info: ValidationInfo):
        context = info.context or {}
        if 'sql_fragments' not in context or not isinstance(value, dict): return value
        value = dict(value)
        if value.get('initial_sql_evidence'):
            raise ValueError('Use initial_sql_refs from the supplied catalogue, not copied SQL text')
        refs = value.get('initial_sql_refs') or []
        catalogue = context['sql_fragments']
        if len(refs) != len(set(refs)) or any(r not in catalogue for r in refs):
            raise ValueError('Unknown or repeated Initial SQL reference')
        if refs:
            value['initial_sql_evidence'] = catalogue[refs[0]] if len(refs)==1 else catalogue['S0']
        return value

    @model_validator(mode="after")
    def grounded_sources(self):
        from .contracts import validate_candidate_evidence
        validate_candidate_evidence(self)
        return self


class CandidateDraft(_CitedEvidence):
    statement: str
    sources: tuple[Literal["memory_graph", "initial_sql"], ...] = ()


class SlotDraft(_Model):
    slot_id: str
    scope: SlotScope
    decision_question: str
    candidates: tuple[CandidateDraft, ...] = Field(
        min_length=1,
        description=(
            "Proposed evidence-bearing implementations. Emit at least two distinct "
            "implementations for a real conflict. Identical statements are merged "
            "by provenance; a proposal left with one implementation is not a "
            "ChoicePoint and is omitted. Never invent an alternative to fill this list."
        ),
    )

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


class ProvenanceAddition(_CitedEvidence):
    pass


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
