"""Selective clarification from explicit constraints and base-column properties."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError

from cortex.application.online_context import (
    ChoicePoint,
    ClarificationFilterDecision,
    InteractionGuidance,
)
from cortex.capabilities.online_context.discovery.evidence import (
    _analysis_graph_payload,
)
from cortex.capabilities.online_context.llm.prompts import load_prompt
from cortex.infrastructure.llm import (
    LLMInvocationError,
    LLMStructuredOutputError,
    run_llm_extraction,
)
from cortex.schema import GraphState


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FilterProbe(_Model):
    choice_point_id: str
    tool: Literal["query_database", "inspect_schema"]
    purpose: str = Field(min_length=1)
    sql: str = ""
    tables: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_arguments(self):
        if self.tool == "query_database" and not self.sql.strip():
            raise ValueError("query_database requires SQL")
        if self.tool == "inspect_schema" and not self.tables:
            raise ValueError("inspect_schema requires explicit table names")
        return self


class FilterStep(_Model):
    requests: tuple[FilterProbe, ...] = ()
    decisions: tuple[ClarificationFilterDecision, ...] = ()

    @model_validator(mode="after")
    def one_action(self):
        if bool(self.requests) == bool(self.decisions):
            raise ValueError("Return tool requests OR final decisions")
        return self


class ClarificationFilterResult(_Model):
    choice_points: tuple[ChoicePoint, ...]
    decisions: tuple[ClarificationFilterDecision, ...] = ()
    guidance: tuple[InteractionGuidance, ...] = ()
    observations: tuple[dict, ...] = ()
    notes: tuple[str, ...] = ()


class ClarificationFilterAgent:
    """One Agent owns both tests; execution failures never establish equivalence."""

    def __init__(
        self,
        llm: BaseChatModel,
        *,
        database=None,
        trace_sink=None,
        max_rounds: int = 6,
        max_probes: int = 8,
    ):
        if max_rounds < 1 or max_probes < 0:
            raise ValueError("invalid clarification filter budgets")
        self.llm, self.database, self.trace_sink = llm, database, trace_sink
        self.max_rounds, self.max_probes = max_rounds, max_probes
        self.context_fingerprint = {
            "version": 2,
            "max_rounds": max_rounds,
            "max_probes": max_probes,
            "database": getattr(database, "context_fingerprint", None),
        }

    def _trace(self, stage, payload):
        if self.trace_sink is not None:
            self.trace_sink(stage, payload)

    async def filter(
        self,
        *,
        question: str,
        initial_sql: str | None,
        retrieved_graph_state: GraphState,
        choice_points: tuple[ChoicePoint, ...],
        dialect: str,
    ) -> ClarificationFilterResult:
        if not choice_points:
            return ClarificationFilterResult(choice_points=())
        points = {p.choice_point_id: p for p in choice_points}
        observations: list[dict] = []
        notes: list[str] = []
        payload = {
            "question": question,
            "initial_sql": initial_sql,
            "dialect": dialect,
            "retrieved_graph_state": _analysis_graph_payload(retrieved_graph_state),
            "slots": [p.model_dump(mode="json") for p in choice_points],
            "database_available": self.database is not None,
            "tools": {
                "inspect_schema": "Read actual metadata for explicit table names.",
                "query_database": (
                    "Generate a read-only probe of base-column properties: NULLs, "
                    "duplicates, formats or value domains. Do not reconstruct the "
                    "initial SQL's unconfirmed business filters or joins."
                ),
            },
        }
        decisions = None
        for round_index in range(self.max_rounds):
            trace = {"round": round_index + 1}
            self._trace("filter_llm", trace)
            try:
                step = await run_llm_extraction(
                    llm=self.llm,
                    prompt=load_prompt("filter_clarifications"),
                    output_model=FilterStep,
                    human_payload={
                        **payload,
                        "observations": observations,
                        "remaining_probes": max(0, self.max_probes - len(observations)),
                        "must_finalize": round_index == self.max_rounds - 1,
                    },
                    trace_capture=trace,
                    repair_with_context=True,
                )
            except (LLMInvocationError, LLMStructuredOutputError) as exc:
                notes.append(f"clarification filter unavailable: {type(exc).__name__}")
                break
            if step.decisions:
                ids = [d.choice_point_id for d in step.decisions]
                if len(ids) == len(points) and set(ids) == set(points):
                    decisions = {d.choice_point_id: d for d in step.decisions}
                else:
                    notes.append(
                        "clarification filter returned incomplete or duplicate decisions"
                    )
                break
            for request in step.requests:
                if len(observations) >= self.max_probes:
                    notes.append("clarification filter probe budget exhausted")
                    break
                observation = await self._probe(request, points, dialect)
                observation["probe_id"] = f"probe:{len(observations) + 1}"
                observations.append(observation)
                self._trace("filter_probe", observation)

        if decisions is None:
            notes.append("filter did not finalize; all questions retained")
            decisions = {}
        kept, final, guidance = [], [], []
        by_probe = {o["probe_id"]: o for o in observations}
        for point in choice_points:
            decision = decisions.get(point.choice_point_id)
            error = self._invalid_decision(decision, point, question, by_probe)
            if error:
                if decision is not None:
                    notes.append(f"filter retained {point.choice_point_id}: {error}")
                decision = ClarificationFilterDecision(
                    choice_point_id=point.choice_point_id,
                    outcome="keep",
                    reason=error,
                )
            final.append(decision)
            if decision.outcome == "keep":
                kept.append(point)
            elif decision.outcome in {"question_resolved", "database_equivalent"}:
                option = next(
                    o
                    for o in point.options
                    if o.option_id == decision.selected_option_id
                )
                prefix = (
                    "The question/evidence explicitly determines this choice."
                    if decision.outcome == "question_resolved"
                    else (
                        "Verified base-column properties eliminate this local difference; "
                        "this does not establish a general business policy."
                    )
                )
                basis = decision.reason
                if decision.outcome == "database_equivalent":
                    basis += (
                        f" Property: {decision.data_property}"
                        f" Applicability: {decision.applicability_reason}"
                    )
                guidance.append(
                    InteractionGuidance(
                        guidance_id=f"filter:{point.choice_point_id}",
                        choice_point_id=point.choice_point_id,
                        decision_question=point.decision_question,
                        should_be=f"{prefix} MUST USE: {option.statement}\nBasis: {basis}",
                        source=decision.outcome,
                    )
                )
        self._trace("filter_decisions", [d.model_dump(mode="json") for d in final])
        return ClarificationFilterResult(
            choice_points=tuple(kept),
            decisions=tuple(final),
            guidance=tuple(guidance),
            observations=tuple(observations),
            notes=tuple(dict.fromkeys(notes)),
        )

    async def _probe(self, request, points, dialect):
        observation = {
            **request.model_dump(mode="json"),
            "success": False,
        }
        if request.choice_point_id not in points:
            return {**observation, "error": "unknown choice point"}
        if self.database is None:
            return {**observation, "error": "database tools unavailable"}
        if self.database.dialect != dialect:
            return {**observation, "error": "database dialect does not match this task"}
        try:
            if request.tool == "inspect_schema":
                operation = asyncio.to_thread(
                    self.database.schema, list(request.tables)
                )
            else:
                operation = asyncio.to_thread(self.database.execute, request.sql)
            result = await asyncio.wait_for(
                operation, timeout=getattr(self.database, "timeout", 8) + 0.5
            )
            return {**observation, "success": True, "result": result}
        except (
            ValueError,
            OSError,
            RuntimeError,
            sqlite3.Error,
            SQLAlchemyError,
        ) as exc:
            return {**observation, "error": str(exc)}

    @staticmethod
    def _invalid_decision(decision, point, question, probes):
        if decision is None:
            return "no validated filter conclusion"
        if decision.outcome in {"keep", "not_relevant"}:
            if decision.selected_option_id is not None:
                return "non-resolution must not select an option"
            return ""
        option_ids = {o.option_id for o in point.options}
        if decision.selected_option_id not in option_ids:
            return "resolution selects an unknown option"
        if decision.outcome == "question_resolved":
            quote = decision.question_evidence.strip()
            if not quote or quote not in question:
                return "explicit constraint needs a verbatim question/evidence anchor"
            excluded = option_ids - {decision.selected_option_id}
            if set(decision.excluded_options) != excluded or any(
                not value.strip() for value in decision.excluded_options.values()
            ):
                return "explicit constraint must explain every alternative"
        else:
            if (
                set(decision.covered_option_ids) != option_ids
                or len(decision.covered_option_ids) != len(option_ids)
                or not decision.data_property.strip()
                or not decision.applicability_reason.strip()
            ):
                return (
                    "equivalence must cover all options, state the base-column property "
                    "and explain why it applies to the local operation"
                )
            evidence = [probes.get(pid) for pid in decision.probe_ids]
            if not evidence or any(
                not o
                or not o["success"]
                or o["choice_point_id"] != point.choice_point_id
                or o["tool"] != "query_database"
                or o["result"].get("truncated", False)
                or o["result"].get("values_truncated", False)
                for o in evidence
            ):
                return "equivalence requires successful, untruncated SQL evidence for this slot"
        return ""
