"""Case domain steps. Nodes return facts; routing lives in graph.py."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.graph.mutations import normalize_groups
from cortex.capabilities.sql_training.graph.snapshots import GraphStore, graph_diff
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
        s.baseline_revision = self.graph.accepted_revision
        if result.direct_completed:
            s.groups = normalize_groups(
                result.proposed_groups, self.graph.accepted, self.graph.working
            )
        else:
            s.stop_case = True
            s.completion_reason = (
                "direct_incomplete"
                if result.outcome == "ready_for_validation"
                else result.outcome
            )

    def validate(self, s: CaseState, *, trial=False, snapshot=None):
        if trial:
            s.iteration += 1
        snapshot = snapshot or self.graph.working
        s.trial_revision = snapshot.revision
        s.validation_ref = self.services.validate(snapshot)
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
        s.completion_reason = None
        s.baseline_revision = self.graph.accepted_revision
        validated = self.evidence.read(s.validation_ref)["payload"]
        # Diagnose the graph that actually produced the failure. A tentative
        # repair is useful evidence even though it has not earned acceptance.
        self.graph.restore(validated["graph_revision"], self.graph.accepted_revision)
        s.targets, s.diagnosis_ref, status = self.services.diagnose(
            self.graph.read(validated["graph_revision"]), s.validation_ref
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
                "validation": self.services.validation_feedback(s.validation_ref),
            }

    def repair(self, s: CaseState):
        # A successful Direct SQL gate may still need knowledge-quality repair.
        if s.round_number == 0:
            s.round_number = 1
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
            # No edit is not a resolution of an observed SQL failure. Let the
            # next outer round diagnose the latest failed candidate instead.
            s.stop_case = self.correct(s)
            s.completion_reason = "unable_to_continue" if changed else "no_progress"

    def validate_trial(self, s: CaseState):
        self.validate(s, trial=True)

    def validate_selected(self, s: CaseState):
        # Selection is verification work, not another Learning repair attempt.
        self.validate(s)

    def assess(self, s: CaseState):
        calls = {}
        try:
            feedback = self.assessor(
                self.services.model,
                record=self.record,
                schema=self.graph.schema,
                baseline=self.graph.read(s.baseline_revision),
                accepted=self.graph.accepted,
                trial=self.graph.read(s.trial_revision),
                groups=s.groups,
                targets=s.targets,
                validation=self.services.compact_validation(s.validation_ref),
                context_limit=self.services.config.train.agent_context_characters,
                trace_capture=calls,
                prior_evidence=self.services.prior_assessment_evidence(s.baseline_revision, s.trial_revision),
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

    def assessed_feedback(self, s: CaseState) -> AssessmentFeedback:
        if s.accepted_revision != self.graph.accepted_revision:
            raise ValueError("assessment accepted revision is stale")
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
        if s.trial_revision != self.graph.working_revision:
            raise ValueError("assessment is not for the current Working candidate")
        trial = self.graph.read(s.trial_revision)
        validated = self.evidence.read(s.validation_ref)["payload"]
        self.services.replay(s.validation_ref)  # verifies case and context
        if validated["content_fingerprint"] != trial.content_fingerprint:
            raise ValueError("validation does not match the assessed candidate")
        feedback = AssessmentFeedback.model_validate(assessed["feedback"])
        feedback.validate_evidence(self.assessment_input(s))
        feedback.accepted_members(s.groups)
        return feedback

    def assessment_input(self, s: CaseState):
        from cortex.capabilities.sql_training.training.assessment_inputs import (
            assessment_payload,
        )
        return assessment_payload(
            record=self.record, schema=self.graph.schema,
            baseline=self.graph.read(s.baseline_revision), accepted=self.graph.accepted,
            trial=self.graph.read(s.trial_revision), groups=s.groups, targets=s.targets,
            validation=self.services.compact_validation(s.validation_ref),
            prior_evidence=self.services.prior_assessment_evidence(s.baseline_revision, s.trial_revision),
        )

    def select(self, s: CaseState):
        feedback = self.assessed_feedback(s)
        filtered_from = s.latest_feedback.get("filtered_from_assessment_ref")
        inputs = self.assessment_input(s)
        s.latest_feedback = {
            **feedback.model_dump(mode="json"),
            "assessment_ref": s.assessment_ref,
            "validation": {"attempts": inputs["attempts"], "evidence_ref": s.validation_ref},
            "stage_facts": [{"unit_id": u["unit_id"], "context_facts": u["context_facts"]} for u in inputs["units"]],
        }
        if filtered_from:
            s.latest_feedback["filtered_from_assessment_ref"] = filtered_from
        members = feedback.accepted_members(s.groups)
        changed = set(graph_diff(self.graph.accepted, self.graph.working))
        if not members or set(members) == changed:
            return
        try:
            selected = self.graph.select(
                assessed_revision=s.trial_revision,
                accepted_before=s.accepted_revision,
                member_ids=members,
            )
        except ValueError as exc:
            s.latest_feedback["selection_error"] = str(exc)
            return
        self.graph.restore(selected.revision, self.graph.accepted_revision)
        s.latest_feedback["filtered_from_assessment_ref"] = s.assessment_ref
        s.groups = normalize_groups(s.groups, self.graph.accepted, selected)
        # The old SQL and assessment remain immutable evidence for the old Trial.
        # Routing sends this strictly smaller candidate through both gates again.

    def accept(self, s: CaseState):
        feedback = self.assessed_feedback(s)
        changed = set(graph_diff(self.graph.accepted, self.graph.working))
        members = feedback.accepted_members(s.groups)
        approved = (
            self.correct(s)
            and feedback.acceptable(s.groups)
            and set(members) == changed
        )
        before = self.graph.accepted_revision
        after = self.graph.working_revision if approved else before
        ref = self.evidence.put(
            "acceptance",
            {
                "baseline_revision": s.baseline_revision,
                "accepted_revision_before": before,
                "assessed_revision": s.trial_revision,
                "validation_ref": s.validation_ref,
                "assessment_ref": s.assessment_ref,
                "policy": assessment_policy_id(),
                "sql_stable_correct": self.correct(s),
                "assessment_acceptable": feedback.acceptable(s.groups),
                "accepted_member_ids": members if approved else [],
                "accepted_revision_after": after,
            },
        )
        self.graph.accepted_revision = after
        s.acceptance_refs.append(ref)
        if approved and s.round_number == 0:
            s.directly_retained_ids = sorted(changed)

    def retain(self, s: CaseState):
        self.graph.retain()

    def validate_final(self, s: CaseState):
        # Only the accepted snapshot can be exported. Unused verified physical
        # facts may remain in Working without altering the accepted graph's proof.
        self.validate(s, snapshot=self.graph.accepted)
        if self.correct(s) and s.direct_completed:
            s.completion_reason = "stable_correct"
        elif self.correct(s):
            s.completion_reason = s.completion_reason or "direct_incomplete"
        elif not s.stop_case and s.round_number >= self.max_rounds:
            s.completion_reason = "round_limit_reached"

    def seal(self, s: CaseState):
        # Close case resources before publishing a merge-eligible result. The outer
        # ExitStack is an idempotent fallback on every exception path.
        from cortex.capabilities.sql_training.artifacts.cases import (
            case_result,
            seal_case,
        )

        if self.graph.accepted.semantic_assets:
            proofs = [self.evidence.read(ref)["payload"] for ref in s.acceptance_refs]
            if not any(
                p["accepted_revision_after"] == self.graph.accepted_revision
                and p["sql_stable_correct"]
                and p["assessment_acceptable"]
                and self.services.replay(p["validation_ref"]).correct
                and self.evidence.read(p["validation_ref"])["payload"][
                    "content_fingerprint"
                ]
                == self.graph.accepted.content_fingerprint
                for p in proofs
            ):
                raise ValueError(
                    "semantic export requires matching SQL and assessment acceptance evidence"
                )
        self.merge_values(self.graph)
        result = case_result(self.record, s, self.graph, self.services, self.root)
        seal_case(self.root, result, self.graph)
        s.result_ref = str(self.root / "result" / "result.json")

    def correct(self, s: CaseState):
        return self.services.replay(s.validation_ref).correct

    def targets_resolved_but_sql_failed(self, s: CaseState):
        if self.correct(s):
            return False
        targets = self.assessed_feedback(s).target_results
        return bool(targets) and all(t.status == "resolved" for t in targets)

    def round_finished(self, s: CaseState):
        return s.iteration >= self.max_iterations or (
            self.correct(s)
            and self.graph.working.content_fingerprint
            == self.graph.accepted.content_fingerprint
        )
