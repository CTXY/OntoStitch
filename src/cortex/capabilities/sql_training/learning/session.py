from __future__ import annotations

import json
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.graph.snapshots import GraphStore
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.config.models import SqlTrainingConfig
from cortex.infrastructure.database import DatabaseConnector


class WorkingNotes(NodeModel):
    current_plan: str = ""
    observations_and_evidence_refs: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    attempted_paths_summary: list[str] = Field(default_factory=list)


class EpisodeResult(NodeModel):
    outcome: Literal["ready_for_validation", "unable_to_continue", "resource_limit"]
    starting_revision: str
    delivered_revision: str
    direct_completed: bool = False
    direct_type_coverage: list[str] = Field(default_factory=list)
    proposed_groups: list[ChangeGroup] = Field(default_factory=list)
    summary: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    database_calls: int = 0
    transcript_ref: str | None = None


@dataclass
class LearningSession:
    mode: Literal["direct", "repair"]
    record: dict[str, Any]
    graph: GraphStore
    evidence: EvidenceStore
    database: DatabaseConnector
    database_url: str
    dialect: str
    config: SqlTrainingConfig
    targets: list[dict[str, Any]] = field(default_factory=list)
    feedback: dict[str, Any] = field(default_factory=dict)
    notes: WorkingNotes = field(default_factory=WorkingNotes)
    groups: list[ChangeGroup] = field(default_factory=list)
    baseline_revision: str | None = None
    remaining_trials: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    database_calls: int = 0
    errors: int = 0
    no_progress: int = 0
    attempt_id: str = field(default_factory=lambda: uuid4().hex)
    closed: bool = False
    stop_reason: str | None = None
    handoff_result: dict[str, Any] | None = None
    observed_reads: set[str] = field(default_factory=set)
    started: float = field(default_factory=monotonic)
    lock: Lock = field(default_factory=Lock)

    def context(self) -> dict[str, Any]:
        snapshot = self.graph.working
        return {
            "mode": self.mode,
            "source": self.record,
            "working_revision": snapshot.revision,
            "accepted_revision": self.graph.accepted_revision,
            "baseline_revision": self.baseline_revision,
            "semantic_nodes": [
                {
                    "id": a.id,
                    "type": a.node_type.value,
                    "name": a.name,
                    "aliases": a.aliases,
                    "references": a.references(),
                }
                for a in snapshot.semantic_assets
            ],
            "targets": self.targets,
            "latest_feedback": self.feedback,
            "working_notes": self.notes.model_dump(mode="json"),
            "budget": {
                "model_calls_left": self.config.agent_max_steps - self.model_calls,
                "database_calls_left": self.config.agent_database_calls
                - self.database_calls,
                "remaining_trials": self.remaining_trials,
            },
        }

    def exhausted(self, *, include_model_limit: bool = True) -> bool:
        return (
            include_model_limit
            and self.model_calls >= self.config.agent_max_steps
            or monotonic() - self.started >= self.config.agent_episode_timeout
            or self.errors >= 3
            or self.no_progress >= 3
        )

    def summarize(self, payload: Any, *, evidence_id: str | None = None) -> Any:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) <= self.config.agent_tool_characters:
            return payload
        evidence_id = evidence_id or self.evidence.put("tool_result", payload)
        return {
            "status": "truncated",
            "evidence_ref": evidence_id,
            "preview": text[: self.config.agent_tool_characters // 2],
            "total_characters": len(text),
            "next_offset": self.config.agent_tool_characters // 2,
            "next_action": "read_evidence with offset",
        }
