"""Run-scoped working memory and a shared budget for consolidation ports and agents."""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from cortex.capabilities.graph_merge.models import ReconcileOutcome
from cortex.infrastructure.llm.diagnostics import exception_details


class ConsolidationBudgetExceeded(RuntimeError):
    pass


active_memory: ContextVar[ConsolidationMemory | None] = ContextVar(
    "graph_merge_consolidation_memory", default=None
)


def fingerprint(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class ConsolidationMemory:
    max_rounds: int = 3
    max_calls: int = 18
    calls: int = 0
    round: int = 0
    analysis: ReconcileOutcome | None = None
    context: dict[str, Any] = field(default_factory=dict)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    failed_candidates: set[str] = field(default_factory=set)
    stop_reason: str = ""
    current_proposal: dict[str, Any] | None = None
    current_review: dict[str, Any] | None = None

    def prompt_payload(self, role: str = "consolidate") -> dict[str, Any]:
        """Expose revisions to the proposer, but no prior verdicts to the independent checker."""
        payload = {"round": self.round, "remaining_calls": self.max_calls - self.calls}
        historical = self.context.get("historical_conflict")
        if historical:
            payload["historical_conflict_points"] = historical.get("conflict_points", [])
        if role == "consolidation_check":
            payload["format_feedback"] = [
                {k: v for k, v in item.items() if k in {"kind", "error_chain", "reason"}}
                for item in self.feedback[-2:]
                if item.get("target") == "checker"
            ]
        else:
            payload["previous_proposal"] = self.current_proposal
            payload["feedback"] = [
                {k: v for k, v in item.items() if k != "review"} for item in self.feedback[-1:]
            ]
            if self.current_review:
                payload["review_feedback"] = {
                    "generalization": self.current_review["generalization"],
                    "consistency": self.current_review["consistency"],
                    **{
                        key: [
                            row
                            for row in self.current_review.get(key, [])
                            if row.get("verdict") != "covered"
                            or row.get("condition_verdict", "covered") != "covered"
                        ]
                        for key in ("verdicts", "source_verdicts", "plan_verdicts")
                    },
                }
        return deepcopy(payload)

    def consume(self, task: str) -> None:
        if self.calls >= self.max_calls:
            self.stop_reason = "call_budget_exhausted"
            raise ConsolidationBudgetExceeded(self.stop_reason)
        self.calls += 1
        self.history.append({"round": self.round, "task": task, "call": self.calls})

    def invoke(self, task, method, *args, **kwargs):
        # Built-in agents debit every internal call. Custom ports debit one call.
        if self.calls >= self.max_calls:
            self.consume(task)
        before = self.calls
        try:
            return method(*args, **kwargs)
        except ConsolidationBudgetExceeded:
            raise
        except Exception as exc:
            members = kwargs.get("representations", ())
            if task == "reconcile" and args:
                members = getattr(args[0], "members", ())
            elif task in {"consolidate", "consolidation_check"}:
                members = getattr(kwargs.get("group"), "members", ())
            self.reject(
                "model_failure",
                refs=tuple(item.ref for item in members),
                task=task,
                target="analysis"
                if task in {"reconcile", "consolidate"}
                else "checker"
                if task == "consolidation_check"
                else "candidate",
                reason=f"{task} call or output validation failed",
                error_chain=exception_details(exc),
            )
            raise
        finally:
            if self.calls == before:
                self.consume(task)

    def record_candidate(self, content, refs, candidate_id, scope_id):
        record = {
            "round": self.round,
            "candidate_id": candidate_id,
            "scope_id": scope_id,
            "supporting_refs": [ref.model_dump(mode="json") for ref in refs],
            "candidate": content.model_dump(mode="json"),
            "review_status": "pending",
            "accepted": False,
        }
        self.history.append(record)
        return record

    def failed_review(self, record):
        record["review_status"] = self.stop_reason or "model_failure"
        if self.feedback:
            record["feedback_index"] = len(self.feedback) - 1

    def reject(self, kind: str, *, refs=(), **details) -> None:
        entry = {
            "kind": kind,
            "supporting_refs": [ref.model_dump(mode="json") for ref in refs],
            **details,
        }
        self.feedback.append(entry)
        self.history.append({"round": self.round, "feedback": entry})

    def audit(self) -> dict[str, Any]:
        """Persist reviews and versions without duplicating the source SQL corpus in the graph."""
        return {key: value for key, value in self.payload().items() if key != "context"}

    def payload(self) -> dict[str, Any]:
        return deepcopy(
            {
                "context": self.context,
                "round": self.round,
                "calls": self.calls,
                "remaining_calls": self.max_calls - self.calls,
                "previous_outcome": self.analysis.model_dump(mode="json")
                if self.analysis
                else None,
                "feedback": self.feedback,
                "history": self.history,
                "stop_reason": self.stop_reason,
                "current_proposal": self.current_proposal,
                "current_review": self.current_review,
            }
        )


def invoke(task, method, *args, **kwargs):
    memory = active_memory.get()
    if memory is None:
        return method(*args, **kwargs)
    return memory.invoke(task, method, *args, **kwargs)
