from __future__ import annotations

import json
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any, Literal

from pydantic import Field

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.graph.snapshots import GraphStore
from cortex.capabilities.sql_training.learning.inputs import (
    AUTHORING_MODELS,
    case_source,
    graph_context,
    semantic_overview,
)
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
    edit_receipts: dict[str, tuple[str, str]] = field(default_factory=dict)
    closed: bool = False
    stop_reason: str | None = None
    stop_detail: str | None = None
    handoff_result: dict[str, Any] | None = None
    observed_reads: set[str] = field(default_factory=set)
    loaded_type_guides: set[str] = field(default_factory=set)
    observed_rejections: set[str] = field(default_factory=set)
    started: float = field(default_factory=monotonic)
    lock: Lock = field(default_factory=Lock)

    def context(self) -> dict[str, Any]:
        snapshot = self.graph.working
        context = graph_context(snapshot, self.record, self.dialect)
        accepted = {a.id: a for a in self.graph.accepted.semantic_assets}
        changed = {a.id for a in snapshot.semantic_assets if accepted.get(a.id) != a}
        pinned = changed | {member for group in self.groups for member in group.member_ids}
        context.update(
            {
                "mode": self.mode,
                "available_semantic_types": list(AUTHORING_MODELS),
                "guidance": "inspect_graph(mode=guide, node_type=...) loads one type contract; mode=relations loads explicit business relations. Choose only evidenced knowledge; no per-type output quota.",
                "database_dialect": self.dialect,
                "source": case_source(self.record),
                "working_revision": snapshot.revision,
                "accepted_revision": self.graph.accepted_revision,
                "semantic_nodes": semantic_overview(
                    snapshot, context["gold_bindings"], str(self.record["question"]), pinned
                ),
                "budget": {
                    **({"model_calls_left": self.config.agent_max_steps - self.model_calls}
                       if self.config.agent_max_steps is not None else {}),
                    "database_calls_left": self.config.agent_database_calls
                    - self.database_calls,
                    "remaining_trials": self.remaining_trials,
                    "read_concurrency": self.config.agent_read_concurrency,
                    **({"repeated_read_decisions_left": max(0, 3 - self.no_progress)}
                       if self.config.agent_max_steps is not None else {}),
                },
            }
        )
        if self.loaded_type_guides:
            context["loaded_type_guides"] = sorted(self.loaded_type_guides)
        if self.no_progress and self.config.agent_max_steps is None:
            context["next_action_hint"] = (
                "Repeated unchanged reads have not advanced the task. Use gathered evidence "
                "to edit the graph or hand off; do not reread unchanged guides."
            )
        elif self.no_progress:
            context["next_action_hint"] = (
                f"{self.no_progress} consecutive repeated-read decisions; "
                f"{max(0, 3 - self.no_progress)} remain before stagnation stops the episode. "
                "Do not reread unchanged guides. Use the evidence already gathered to submit "
                "the smallest supported coherent edit with explicit edges, or call handoff "
                "with the concrete completion gap if unable to proceed."
            )
        if self.no_progress >= 2:
            context["next_action_hint"] += (
                " Only edit_graph and handoff are available for this decision. "
                "Save only evidenced knowledge; otherwise hand off unable_to_continue."
            )
        if self.mode == "repair":
            if self.targets:
                context["targets"] = self.targets
            if self.feedback:
                context["latest_feedback"] = self.feedback
        notes = {
            key: value for key, value in self.notes.model_dump(mode="json").items() if value
        }
        if notes:
            context["working_notes"] = notes
        return context

    def exhausted(self, *, include_model_limit: bool = True) -> bool:
        bounded = self.config.agent_max_steps is not None
        return (
            (include_model_limit and bounded and self.model_calls >= self.config.agent_max_steps)
            or (self.config.agent_episode_timeout is not None
                and monotonic() - self.started >= self.config.agent_episode_timeout)
            or (bounded and (self.errors >= 3 or self.no_progress >= 3))
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
