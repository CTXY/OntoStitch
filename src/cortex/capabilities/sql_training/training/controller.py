"""The only outer case state machine. Learning autonomy stays inside episodes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
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
from cortex.capabilities.sql_training.training.observability import SummaryTracer
from cortex.capabilities.sql_training.training.services import CaseServices
from cortex.capabilities.sql_training.training.state import CaseState
from cortex.infrastructure.database import DatabaseConnector


class CaseController:
    def __init__(
        self,
        *,
        root: Path,
        graph: GraphStore,
        evidence: EvidenceStore,
        services: CaseServices,
        agent: LearningAgent,
        database: DatabaseConnector,
        record: dict[str, Any],
        max_rounds: int,
        max_iterations: int,
        skip_direct: bool = False,
        assessor: Callable[..., AssessmentFeedback] = assess,
    ):
        self.root, self.graph, self.evidence = root, graph, evidence
        self.services, self.agent, self.database = services, agent, database
        self.record = record
        self.max_rounds, self.max_iterations = max_rounds, max_iterations
        self.skip_direct, self.assessor = skip_direct, assessor
        self.checkpoint_path = root / "checkpoint.json"
        self.tracer = SummaryTracer()
        self.state = CaseState(
            working_revision=graph.working_revision,
            accepted_revision=graph.accepted_revision,
            context_id=services.context_id,
            phase="S2" if skip_direct else "S1",
            direct_completed=skip_direct,
        )

    def run(self, *, resume: bool = False) -> CaseState:
        if resume:
            self.state = CaseState.model_validate(read_json(self.checkpoint_path))
            if self.state.context_id != self.services.context_id:
                raise ValueError("cannot resume a case with changed source/data/config")
            self.graph.restore(
                self.state.working_revision, self.state.accepted_revision
            )
            if self.state.phase == "done":
                return self.state
            if self.state.resume_count >= 3:
                raise ValueError("case recovery retry limit reached")
            self.state.resume_count += 1
        self.checkpoint()
        while self.state.phase not in {"S9", "done"}:
            try:
                with self.tracer.span(
                    f"extract.{self.state.phase}",
                    case_id=self.record["source_id"],
                    round=self.state.round_number,
                    iteration=self.state.iteration,
                    context_id=self.services.context_id,
                    schema_version=self.graph.schema_version,
                    model=self.services.config.llm.resolve("sql_training").model,
                ) as summary:
                    self.step()
                    summary.update(
                        revision=self.graph.working_revision,
                        evidence_ref=self.state.validation_ref,
                    )
            except Exception as exc:  # noqa: BLE001 - isolate run/case failures and preserve accepted assets
                # Keep the last accepted pointer and evidence even after a runtime
                # fault. A failed attempt cannot roll accepted improvements back.
                self.evidence.put(
                    "operational_failure",
                    {
                        "phase": self.state.phase,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                self.state.operational_failure = True
                self.state.stop_case = True
                self.state.completion_reason = "operational_failure"
                if self.state.phase in {"S7", "S8"}:
                    # No recursive finalization when schema/storage/data itself is broken.
                    self.state.phase = "S9"
                else:
                    self.state.phase = "S7"
                self.checkpoint()
        return self.state

    def checkpoint(self) -> None:
        self.state.working_revision = self.graph.working_revision
        self.state.accepted_revision = self.graph.accepted_revision
        write_json_atomic(self.checkpoint_path, self.state.model_dump(mode="json"))

    def step(self) -> None:
        s = self.state
        phase = s.phase
        if phase == "S1":
            result = self.episode("direct")
            s.direct_completed = result.direct_completed
            if result.direct_completed:
                self.graph.retain_direct()
                s.directly_retained_ids = [
                    a.id for a in self.graph.accepted.semantic_assets
                ]
                s.events.append(
                    {
                        "stage": "initial_direct_extraction",
                        "round": 0,
                        "retention_basis": "direct_extraction",
                        "retained_asset_ids": s.directly_retained_ids,
                    }
                )
                s.phase = "S2"
            else:
                s.stop_case = True
                s.completion_reason = (
                    result.outcome
                    if result.outcome != "ready_for_validation"
                    else "direct_incomplete"
                )
                s.phase = "S7"
        elif phase == "S2":
            s.validation_ref = self.services.validate(self.graph.accepted)
            replay = self.services.replay(s.validation_ref)
            if replay.correct:
                s.phase = "S7"
            elif not self.can_diagnose():
                self.stop_unavailable()
            else:
                s.phase = "S3"
        elif phase == "S3":
            if s.stop_case or s.round_number >= self.max_rounds:
                s.completion_reason = s.completion_reason or "round_limit_reached"
                s.phase = "S7"
            else:
                s.round_number += 1
                s.iteration = 0
                s.baseline_revision = self.graph.accepted_revision
                s.targets, s.diagnosis_ref, diagnosis_status = self.services.diagnose(
                    self.graph.accepted, s.validation_ref
                )
                if not s.targets:
                    s.stop_case = True
                    s.completion_reason = (
                        "diagnosis_inconclusive"
                        if diagnosis_status
                        in {
                            "analysis_incomplete",
                            "repair_invalid",
                            "unavailable",
                            "oracle_unavailable",
                            "execution_failed",
                        }
                        else "no_confirmed_targets"
                    )
                    if diagnosis_status in {"unavailable", "oracle_unavailable"}:
                        s.operational_failure = True
                    s.phase = "S7"
                else:
                    s.latest_feedback = {
                        "diagnosis_ref": s.diagnosis_ref,
                        "validation": self.services.compact_validation(
                            s.validation_ref
                        ),
                    }
                    s.groups = []
                    s.phase = "S4"
        elif phase == "S4":
            before = self.graph.working
            result = self.episode("repair")
            changed = (
                self.graph.working.content_fingerprint != before.content_fingerprint
            )
            if result.outcome == "unable_to_continue" or not changed:
                s.completion_reason = "unable_to_continue" if changed else "no_progress"
                s.phase = "S7"
            elif s.iteration >= self.max_iterations:
                s.phase = "S7"
            else:
                s.groups = normalize_groups(
                    result.proposed_groups, self.graph.accepted, self.graph.working
                )
                s.phase = "S5"
        elif phase == "S5":
            s.iteration += 1
            s.validation_ref = self.services.validate(self.graph.working)
            s.events.append(
                {
                    "round": s.round_number,
                    "iteration": s.iteration,
                    "trial_revision": self.graph.working_revision,
                    "validation_ref": s.validation_ref,
                }
            )
            if not self.can_assess():
                self.stop_unavailable()
            else:
                s.phase = "S6"
        elif phase == "S6":
            before = self.graph.accepted_revision
            trial = self.graph.working
            assessment_trace = {}
            try:
                feedback = self.assessor(
                    self.services.model,
                    record=self.record,
                    baseline=self.graph.read(s.baseline_revision),
                    accepted=self.graph.accepted,
                    trial=trial,
                    groups=s.groups,
                    targets=s.targets,
                    validation=self.services.compact_validation(s.validation_ref),
                    context_limit=self.services.config.train.agent_context_characters,
                    trace_capture=assessment_trace,
                )
            finally:
                trace_ref = (
                    self.evidence.put("assessment_model_calls", assessment_trace)
                    if assessment_trace
                    else None
                )
            members = feedback.accepted_members(s.groups)
            promoted = self.graph.accepted
            promotion_error = None
            if members:
                try:
                    promoted = self.graph.promote(
                        assessed_revision=trial.revision,
                        accepted_before=before,
                        member_ids=members,
                        publish=False,
                    )
                except ValueError as exc:
                    # A valid W may yield an invalid projected subset; keep L and
                    # return concrete feedback, without subset search or another assessor.
                    members = []
                    promotion_error = str(exc)
            record = {
                "baseline_revision": s.baseline_revision,
                "accepted_revision_before": before,
                "assessed_revision": trial.revision,
                "validation_ref": s.validation_ref,
                "policy": assessment_policy_id(),
                "model_trace_ref": trace_ref,
                "groups": [g.model_dump(mode="json") for g in s.groups],
                "feedback": feedback.model_dump(mode="json"),
                "accepted_member_ids": members,
                "accepted_revision_after": promoted.revision,
            }
            ref = self.evidence.put("acceptance", record)
            self.graph.accepted_revision = promoted.revision
            s.acceptance_refs.append(ref)
            s.latest_feedback = {
                **feedback.model_dump(mode="json"),
                "assessment_ref": ref,
                "promotion_error": promotion_error,
            }
            resolved = bool(feedback.target_results) and all(
                t.status == "resolved" for t in feedback.target_results
            )
            if (
                self.services.replay(s.validation_ref).correct
                or resolved
                or s.iteration >= self.max_iterations
            ):
                s.phase = "S7"
            else:
                s.phase = "S4"
        elif phase == "S7":
            self.graph.retain()
            s.phase = "S8"
        elif phase == "S8":
            s.validation_ref = self.services.validate(self.graph.working)
            replay = self.services.replay(s.validation_ref)
            if replay.correct:
                s.completion_reason = (
                    "stable_correct"
                    if not s.operational_failure
                    else "operational_failure"
                )
                s.phase = "S9"
            elif not self.can_diagnose():
                self.stop_unavailable(final=True)
            elif s.stop_case or s.round_number >= self.max_rounds:
                s.completion_reason = s.completion_reason or "round_limit_reached"
                s.phase = "S9"
            else:
                s.phase = "S3"
        else:
            raise ValueError(f"unknown case phase: {phase}")
        self.checkpoint()

    def episode(self, mode):
        s = self.state
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
        s.events.append(
            {
                "stage": mode + "_episode",
                "round": s.round_number,
                "episode": result.model_dump(mode="json"),
            }
        )
        return result

    def can_assess(self) -> bool:
        replay = self.services.replay(self.state.validation_ref)
        return replay.terminal_evaluation.get(
            "evaluation_available"
        ) is not False and bool(replay.terminal_evaluation.get("query"))

    def can_diagnose(self) -> bool:
        return self.can_assess()

    def stop_unavailable(self, *, final=False) -> None:
        self.state.stop_case = True
        self.state.operational_failure = True
        self.state.completion_reason = "validation_unavailable"
        self.state.phase = "S9" if final else "S7"
