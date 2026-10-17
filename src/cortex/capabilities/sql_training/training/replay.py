from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import Field

from cortex.capabilities.sql_training.models.base import NodeModel


class ReplayGateStatus(StrEnum):
    STABLE_CORRECT = "stable_correct"
    INCORRECT = "incorrect"
    UNAVAILABLE = "unavailable"


class ReplayAttempt(NodeModel):
    attempt: int = Field(ge=1)
    trace_id: str = Field(min_length=1)
    evaluation: dict[str, Any]
    intent_output: dict[str, Any] | None = None
    selected_ontology_ids: list[str] = Field(default_factory=list)
    raw_retrieved_ontology_ids: list[str] = Field(default_factory=list)


class StableReplayResult(NodeModel):
    status: ReplayGateStatus
    correct: bool
    required_correct_attempts: int = Field(ge=1)
    attempts: list[ReplayAttempt] = Field(min_length=1)
    terminal_evaluation: dict[str, Any]
    first_failure_attempt: int | None = None

    @property
    def terminal_attempt(self) -> ReplayAttempt:
        return self.attempts[-1].model_copy(deep=True)

    def effective_selected_ontology_ids(self) -> set[str]:
        """Return stable retrieval evidence, or the terminal failure evidence."""

        if self.status != ReplayGateStatus.STABLE_CORRECT:
            return set(self.attempts[-1].selected_ontology_ids)
        selected = set(self.attempts[0].selected_ontology_ids)
        for attempt in self.attempts[1:]:
            selected.intersection_update(attempt.selected_ontology_ids)
        return selected

    def consistently_selected_ontology_ids(self) -> set[str]:
        """Return ontology IDs selected in every completed replay attempt."""

        selected = set(self.attempts[0].selected_ontology_ids)
        for attempt in self.attempts[1:]:
            selected.intersection_update(attempt.selected_ontology_ids)
        return selected

    def effective_raw_retrieved_ontology_ids(self) -> set[str]:
        """Return stable raw-recall evidence, or the terminal failure evidence."""

        if self.status != ReplayGateStatus.STABLE_CORRECT:
            return set(self.attempts[-1].raw_retrieved_ontology_ids)
        retrieved = set(self.attempts[0].raw_retrieved_ontology_ids)
        for attempt in self.attempts[1:]:
            retrieved.intersection_update(attempt.raw_retrieved_ontology_ids)
        return retrieved


def run_stable_replay(
    attempt_runner: Callable[[int], ReplayAttempt],
    *,
    required_correct_attempts: int,
) -> StableReplayResult:
    """Require consecutive correct ChatBI replays and stop at the first failure."""

    if required_correct_attempts < 1:
        raise ValueError("required_correct_attempts must be >= 1")

    attempts: list[ReplayAttempt] = []
    for attempt_number in range(1, required_correct_attempts + 1):
        attempt = attempt_runner(attempt_number)
        if attempt.attempt != attempt_number:
            raise ValueError("replay attempt runner returned an unexpected attempt number")
        attempts.append(attempt.model_copy(deep=True))
        evaluation = attempt.evaluation
        if evaluation.get("evaluation_available") is False:
            return _build_result(
                ReplayGateStatus.UNAVAILABLE,
                attempts,
                required_correct_attempts=required_correct_attempts,
                first_failure_attempt=attempt_number,
            )
        if not evaluation.get("correct"):
            return _build_result(
                ReplayGateStatus.INCORRECT,
                attempts,
                required_correct_attempts=required_correct_attempts,
                first_failure_attempt=attempt_number,
            )

    return _build_result(
        ReplayGateStatus.STABLE_CORRECT,
        attempts,
        required_correct_attempts=required_correct_attempts,
        first_failure_attempt=None,
    )


def _build_result(
    status: ReplayGateStatus,
    attempts: list[ReplayAttempt],
    *,
    required_correct_attempts: int,
    first_failure_attempt: int | None,
) -> StableReplayResult:
    terminal_evaluation = dict(attempts[-1].evaluation)
    terminal_evaluation.update(
        {
            "correct": status == ReplayGateStatus.STABLE_CORRECT,
            "replay_status": status.value,
            "required_correct_replays": required_correct_attempts,
            "completed_replays": len(attempts),
            "first_failure_attempt": first_failure_attempt,
        }
    )
    return StableReplayResult(
        status=status,
        correct=status == ReplayGateStatus.STABLE_CORRECT,
        required_correct_attempts=required_correct_attempts,
        attempts=attempts,
        terminal_evaluation=terminal_evaluation,
        first_failure_attempt=first_failure_attempt,
    )
