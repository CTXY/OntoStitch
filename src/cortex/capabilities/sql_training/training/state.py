"""Trusted case execution facts; working notes live separately."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.learning.session import WorkingNotes
from cortex.capabilities.sql_training.models.base import NodeModel


class CaseState(NodeModel):
    phase: Literal["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "done"] = "S1"
    working_revision: str
    accepted_revision: str
    context_id: str
    baseline_revision: str | None = None
    round_number: int = 0
    iteration: int = 0
    targets: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[ChangeGroup] = Field(default_factory=list)
    notes: WorkingNotes = Field(default_factory=WorkingNotes)
    latest_feedback: dict[str, Any] = Field(default_factory=dict)
    validation_ref: str | None = None
    diagnosis_ref: str | None = None
    acceptance_refs: list[str] = Field(default_factory=list)
    directly_retained_ids: list[str] = Field(default_factory=list)
    direct_completed: bool = False
    stop_case: bool = False
    completion_reason: str | None = None
    operational_failure: bool = False
    model_calls: int = 0
    tool_calls: int = 0
    database_calls: int = 0
    resume_count: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)
