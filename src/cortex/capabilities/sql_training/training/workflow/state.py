"""Serializable case facts. Execution position belongs only to LangGraph."""

from typing import Any

from pydantic import Field

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.learning.session import WorkingNotes
from cortex.capabilities.sql_training.models.base import NodeModel

WORKFLOW_VERSION = "case-graph-v7-failure-rediagnosis"


class CaseState(NodeModel):
    workflow_version: str = WORKFLOW_VERSION
    case_id: str
    context_id: str
    working_revision: str
    accepted_revision: str
    baseline_revision: str | None = None
    round_number: int = 0
    iteration: int = 0
    targets: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[ChangeGroup] = Field(default_factory=list)
    notes: WorkingNotes = Field(default_factory=WorkingNotes)
    latest_feedback: dict[str, Any] = Field(default_factory=dict)
    validation_ref: str | None = None
    diagnosis_ref: str | None = None
    assessment_ref: str | None = None
    trial_revision: str | None = None
    episode_result_ref: str | None = None
    acceptance_refs: list[str] = Field(default_factory=list)
    directly_retained_ids: list[str] = Field(default_factory=list)
    direct_completed: bool = False
    candidate_ready: bool = False
    stop_case: bool = False
    completion_reason: str | None = None
    operational_failure: bool = False
    model_calls: int = 0
    tool_calls: int = 0
    database_calls: int = 0
    events: list[dict[str, Any]] = Field(default_factory=list)
    result_ref: str | None = None
