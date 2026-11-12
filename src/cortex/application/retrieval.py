"""Shared retrieval evidence contracts, independent of user interaction."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RepresentationKind = Literal[
    "canonical_node",
    "conflict_alternative",
    "concept_target",
    "metric_expression",
    "metric_plan",
    "generation_rule",
]
RelevanceStatus = Literal["relevant", "uncertain", "irrelevant"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalDocument(_Model):
    document_id: str
    canonical_node_id: str
    node_type: str
    representation_kind: RepresentationKind
    alternative_id: str | None = None
    target_id: str | None = None
    plan_id: str | None = None
    searchable_text: str
    structured_signals: dict[str, Any] = Field(default_factory=dict)


class ColumnSemanticContext(_Model):
    column_id: str
    table_name: str
    column_name: str
    business_name: str = ""
    description: str = ""
    data_format: str = ""
    value_description: str = ""


class RetrievedRepresentation(_Model):
    document: RetrievalDocument
    score: float = Field(ge=0, le=1)
    channels: tuple[str, ...] = ()


class RelevanceDecision(_Model):
    document_id: str
    status: RelevanceStatus
    reason: str
