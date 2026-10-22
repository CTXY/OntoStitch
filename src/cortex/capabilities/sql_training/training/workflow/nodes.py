"""Case domain steps. Nodes return facts; routing lives in graph.py."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.graph.mutations import normalize_groups
from cortex.capabilities.sql_training.graph.snapshots import GraphStore
from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.learning.session import LearningSession
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.capabilities.sql_training.training.feedback import (
    AssessmentFeedback,
    assess,
    assessment_policy_id,
)
from cortex.capabilities.sql_training.training.services import CaseServices
from cortex.capabilities.sql_training.training.workflow.state import CaseState
from cortex.infrastructure.database import DatabaseConnector


class ValidationUnavailable(RuntimeError):
    """An infrastructure fault, never a semantic failure or normal completion."""


@dataclass
class CaseRuntime:
    root: Path
    graph: GraphStore
    evidence: EvidenceStore
    services: CaseServices
    agent: LearningAgent
    database: DatabaseConnector
    record: dict[str, Any]
    max_rounds: int
    max_iterations: int
    assessor: Callable[..., AssessmentFeedback] = assess
    close_resources: Callable[[], None] = field(default=lambda: None)
    merge_values: Callable[[GraphStore], None] = field(default=lambda graph: None)

    def episode(self, s: CaseState, mode: str):
        session = LearningSession(
            mode=mode,
            record=self.record,
            graph=self.graph,
            evidence=self.evidence,
            database=self.database,
            database_url=self.services.config.database.url or "",
            dialect=self.services.config.database.dialect,
            config=self.services.config.train,
            targets=s.targets,
            feedback=s.latest_feedback,
            notes=s.notes.model_copy(deep=True),
            groups=list(s.groups),
            baseline_revision=s.baseline_revision,
            remaining_trials=max(0, self.max_iterations - s.iteration),
        )
        result = self.agent.run(session)
        s.notes = session.notes
        s.model_calls += result.model_calls
        s.tool_calls += result.tool_calls
        s.database_calls += result.database_calls
        s.episode_result_ref = self.evidence.put(
            "episode_result", result.model_dump(mode="json")
        )
        s.events.append(
            {
                "stage": mode + "_episode",
                "round": s.round_number,
                "episode_ref": s.episode_result_ref,
            }
        )
        return result

    def direct(self, s: CaseState):
        result = self.episode(s, "direct")
        s.direct_completed = result.direct_completed
        if result.direct_completed:
            self.graph.retain_direct()
            s.directly_retained_ids = [
                a.id for a in self.graph.accepted.semantic_assets
            ]
        else:
            s.stop_case = True
            s.completion_reason = (
                "direct_incomplete"
                if result.outcome == "ready_for_validation"
                else result.outcome
            )

    def validate(self, s: CaseState, *, trial=False):
        if trial:
            s.iteration += 1
            s.trial_revision = self.graph.working_revision
        s.validation_ref = self.services.validate(self.graph.working)
        replay = self.services.replay(s.validation_ref)
        if replay.status.value == "unavailable":
            raise ValidationUnavailable(
                "validation infrastructure unavailable; resume this case after recovery"
            )
        if not str(replay.terminal_evaluation.get("query") or "").strip():
            # An empty model result is a completed semantic generation failure,
            # not an infrastructure outage and not evidence for assessment.
            s.stop_case = True
            s.completion_reason = "candidate_generation_failed"
        if trial:
            s.events.append(
                {
                    "round": s.round_number,
                    "iteration": s.iteration,
                    "trial_revision": s.trial_revision,
                    "validation_ref": s.validation_ref,
                }
            )

    def validate_initial(self, s: CaseState):
        self.validate(s)

    def diagnose(self, s: CaseState):
        s.round_number += 1
        s.iteration = 0
        s.baseline_revision = self.graph.accepted_revision
        s.targets, s.diagnosis_ref, status = self.services.diagnose(
            self.graph.accepted, s.validation_ref
        )
        if status in {"unavailable", "oracle_unavailable"}:
            raise ValidationUnavailable("diagnosis infrastructure unavailable")
        if not s.targets:
            s.stop_case = True
            s.completion_reason = (
                "diagnosis_inconclusive"
                if status
                in {"analysis_incomplete", "repair_invalid", "execution_failed"}
                else "no_confirmed_targets"
            )
        else:
            s.groups = []
            s.latest_feedback = {
                "diagnosis_ref": s.diagnosis_ref,
                "validation": self.services.compact_validation(s.validation_ref),
            }

    def repair(self, s: CaseState):
        before = self.graph.working.content_fingerprint
        result = self.episode(s, "repair")
        changed = before != self.graph.working.content_fingerprint
        s.candidate_ready = (
            changed
            and result.outcome != "unable_to_continue"
            and s.iteration < self.max_iterations
        )
        if s.candidate_ready:
            s.groups = normalize_groups(
                result.proposed_groups, self.graph.accepted, self.graph.working
            )
        else:
            s.stop_case = True
            s.completion_reason = "unable_to_continue" if changed else "no_progress"

    def validate_trial(self, s: CaseState):
        self.validate(s, trial=True)

    def assess(self, s: CaseState):
        calls = {}
        try:
            feedback = self.assessor(
                self.services.model,
                record=self.record,
                baseline=self.graph.read(s.baseline_revision),
                accepted=self.graph.accepted,
                trial=self.graph.read(s.trial_revision),
                groups=s.groups,
                targets=s.targets,
                validation=self.services.compact_validation(s.validation_ref),
                context_limit=self.services.config.train.agent_context_characters,
                trace_capture=calls,
            )
        finally:
            trace_ref = (
                self.evidence.put("assessment_model_calls", calls) if calls else None
            )
        s.assessment_ref = self.evidence.put(
            "assessment",
            {
                "baseline_revision": s.baseline_revision,
                "accepted_before": s.accepted_revision,
                "trial_revision": s.trial_revision,
                "validation_ref": s.validation_ref,
                "groups": [g.model_dump(mode="json") for g in s.groups],
                "feedback": feedback.model_dump(mode="json"),
                "model_trace_ref": trace_ref,
            },
        )

    def accept(self, s: CaseState):
        assessed = self.evidence.read(s.assessment_ref)["payload"]
        expected = {
            "baseline_revision": s.baseline_revision,
            "accepted_before": s.accepted_revision,
            "trial_revision": s.trial_revision,
            "validation_ref": s.validation_ref,
            "groups": [g.model_dump(mode="json") for g in s.groups],
        }
        if any(assessed[k] != v for k, v in expected.items()):
            raise ValueError("assessment does not match acceptance inputs")
        feedback = AssessmentFeedback.model_validate(assessed["feedback"])
        members = feedback.accepted_members(s.groups)
        promoted = self.graph.accepted
        error = None
        if members:
            try:
                promoted = self.graph.promote(
                    assessed_revision=s.trial_revision,
                    accepted_before=s.accepted_revision,
                    member_ids=members,
                    publish=False,
                )
            except ValueError as exc:
                members, error = [], str(exc)
        ref = self.evidence.put(
            "acceptance",
            {
                "baseline_revision": s.baseline_revision,
                "accepted_revision_before": s.accepted_revision,
                "assessed_revision": s.trial_revision,
                "validation_ref": s.validation_ref,
                "assessment_ref": s.assessment_ref,
                "policy": assessment_policy_id(),
                "accepted_member_ids": members,
                "accepted_revision_after": promoted.revision,
            },
        )
        self.graph.accepted_revision = promoted.revision
        s.acceptance_refs.append(ref)
        s.latest_feedback = {
            **feedback.model_dump(mode="json"),
            "assessment_ref": s.assessment_ref,
            "promotion_error": error,
        }

    def retain(self, s: CaseState):
        self.graph.retain()

    def validate_final(self, s: CaseState):
        self.validate(s)
        if self.correct(s):
            s.completion_reason = "stable_correct"
        elif not s.stop_case and s.round_number >= self.max_rounds:
            s.completion_reason = "round_limit_reached"

    def seal(self, s: CaseState):
        # Close case resources before publishing a merge-eligible result. The outer
        # ExitStack is an idempotent fallback on every exception path.
        from cortex.capabilities.sql_training.artifacts.cases import (
            case_result,
            seal_case,
        )

        self.merge_values(self.graph)
        result = case_result(self.record, s, self.graph, self.services, self.root)
        seal_case(self.root, result, self.graph)
        s.bundle_ref = str(self.root / "bundle" / "case_result.json")

    def correct(self, s: CaseState):
        return self.services.replay(s.validation_ref).correct

    def round_finished(self, s: CaseState):
        targets = s.latest_feedback.get("target_results", [])
        return (
            self.correct(s)
            or s.iteration >= self.max_iterations
            or bool(targets)
            and all(t["status"] == "resolved" for t in targets)
        )
