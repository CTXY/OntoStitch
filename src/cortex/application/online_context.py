from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cortex.application.common import ApplicationError
from cortex.application.retrieval import (
    ColumnSemanticContext,
    RelevanceDecision,
    RetrievalDocument,
    RetrievedRepresentation,
)
from cortex.schema import (
    GraphSpace,
    GraphState,
    NodeMergeConflict,
    SchemaDefinition,
    export_inline_graph,
)

EvidenceSourceType = Literal["initial_sql", "ontology_asset", "database_schema"]
CandidateApplicability = Literal["applicable", "uncertain"]
SourceCategory = Literal["H", "M", "L"]
EvidencePriority = Literal["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8", "p9"]
SlotScope = Literal[
    "output",
    "filter",
    "join",
    "calculation",
    "grouping",
    "ordering",
    "row_selection",
    "set_logic",
    "binding",
    "other",
]
OnlineContextStatus = Literal[
    "completed",
    "completed_with_custom_guidance",
    "completed_with_unresolved",
    "no_match",
    "unavailable",
]


class OnlineContextError(ApplicationError):
    """Raised when online context construction violates its public contract."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateEvidenceAssessment(_Model):
    source_types: tuple[EvidenceSourceType, ...] = Field(min_length=1)
    applicability: CandidateApplicability = Field(
        description="applicable: directly matches the requested role and conditions (1.0); "
        "uncertain: relevant implementation guidance from a related context (0.5)."
    )
    explanation: str = Field(min_length=1)

    @property
    def relevance_score(self) -> float:
        """Paper's a(c), retaining the existing categorical LLM output contract."""
        return 1.0 if self.applicability == "applicable" else 0.5


class InteractionOption(_Model):
    option_id: str
    statement: str
    candidate_sources: tuple[Literal["memory_graph", "initial_sql"], ...] = Field(
        min_length=1
    )
    graph_refs: tuple[str, ...] = ()
    initial_sql_evidence: str | None = None
    evidence_assessment: CandidateEvidenceAssessment | None = None

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        expected = set()
        if self.graph_refs:
            expected.add("memory_graph")
        if self.initial_sql_evidence:
            expected.add("initial_sql")
        if set(self.candidate_sources) != expected:
            raise ValueError(
                "candidate sources must match graph and initial SQL evidence"
            )
        return self


class ClarificationPriority(_Model):
    # P1-P9 are read only for historical evaluation artifacts.
    evidence_priority: EvidencePriority | None = None
    evidence_priority_reason: str = ""
    priority_reason: str = ""
    source_category: SourceCategory | None = None
    applicable_candidate_count: int | None = Field(default=None, ge=0)
    candidate_relevance_sum: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    priority_score: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class ChoicePoint(ClarificationPriority):
    choice_point_id: str
    scope: SlotScope = "other"
    decision_question: str
    options: tuple[InteractionOption, ...] = Field(min_length=2)
    selection_mode: Literal["multiple"] = "multiple"


class InteractionQuestion(ClarificationPriority):
    question_id: str
    choice_point_ids: tuple[str, ...] = Field(min_length=1)
    scope: SlotScope = "other"
    prompt: str
    selection_mode: Literal["multiple"] = "multiple"
    options: tuple[InteractionOption, ...] = Field(min_length=2)
    requires_explanation_when_no_option: Literal[True] = True


class InteractionAnswer(_Model):
    question_id: str
    selected_option_ids: tuple[str, ...] = ()
    explanation: str = ""

    @model_validator(mode="after")
    def require_choice_or_explanation(self) -> Self:
        if not self.selected_option_ids and not self.explanation.strip():
            raise ValueError("an interaction answer requires an option or explanation")
        if len(self.selected_option_ids) != len(set(self.selected_option_ids)):
            raise ValueError("interaction answer option IDs must be unique")
        return self


class InteractionGuidance(ClarificationPriority):
    guidance_id: str
    choice_point_id: str = ""
    decision_question: str
    should_be: str
    must_not_be: tuple[str, ...] = ()
    source: Literal[
        "user_choice",
        "user_explanation",
        "question_resolved",
        "ownership_contract",
    ]

    def render(self) -> str:
        prefix = {
            "user_choice": "经过用户选择",
            "user_explanation": "根据用户说明",
            "question_resolved": "根据问题的明确要求",
            "ownership_contract": "根据资产职责约束",
        }[self.source]
        base = (
            f"问题中的「{self.decision_question}」，{prefix}应该是「{self.should_be}」"
        )
        if self.must_not_be:
            excluded = "、".join(f"「{item}」" for item in self.must_not_be)
            return f"{base}，而禁止{excluded}。"
        return f"{base}。"


class QueryExample(_Model):
    """An original historical Case supporting one or more relevant assets."""

    source_id: str
    question: str = ""
    sql: str | None = None
    support_count: int = 0
    matched_asset_ids: tuple[str, ...] = ()


class DownstreamGuidance(_Model):
    """A terminal, directly consumable handoff for a downstream SQL agent."""

    usage: str
    guidance_content: str
    ontology_assets: tuple[dict[str, Any], ...] = ()
    interaction_guidance: tuple[InteractionGuidance, ...] = ()
    resolved_join_paths: tuple[str, ...] = ()
    query_examples: tuple[QueryExample, ...] = ()


class ProjectionDecision(_Model):
    choice_point_id: str
    selected_option_ids: tuple[str, ...]
    retained_graph_refs: tuple[str, ...] = ()
    removed_graph_refs: tuple[str, ...] = ()


class OnlineContextPending(_Model):
    run_id: str | None = None
    workflow_fingerprint: str | None = None
    stage: Literal["clarification", "non_join", "join"] = "clarification"
    remaining_budget: int | None = Field(default=None, ge=0)
    question: str
    dialect: str = "mysql"
    initial_sql: str | None
    graph_state_fingerprint: str
    selected_node_ids: tuple[str, ...]
    relevant_semantic_node_ids: tuple[str, ...] = ()
    relevant_grounding_node_ids: tuple[str, ...] = ()
    resolved_join_paths: tuple[str, ...] = ()
    prior_answers: tuple[InteractionAnswer, ...] = ()
    choice_points: tuple[ChoicePoint, ...]
    questions: tuple[InteractionQuestion, ...]


class OnlineContextDiagnostics(_Model):
    recalled_documents: int = 0
    relevant_documents: int = 0
    uncertain_documents: int = 0
    conflict_points: int = 0
    presented_questions: int = 0
    prioritization_policy: Literal["source_score_v1", "source_score_v2"] | None = None
    notes: tuple[str, ...] = ()


class OnlineContextResult(_Model):
    status: OnlineContextStatus | None
    question: str
    initial_sql: str | None = None
    graph_state_fingerprint: str
    retrieved_node_ids: tuple[str, ...] = ()
    retrieved_conflict_node_ids: tuple[str, ...] = ()
    selected_node_ids: tuple[str, ...] = ()
    relevant_semantic_node_ids: tuple[str, ...] = ()
    relevant_grounding_node_ids: tuple[str, ...] = ()
    representations: tuple[RetrievedRepresentation, ...] = ()
    relevance: tuple[RelevanceDecision, ...] = ()
    choice_points: tuple[ChoicePoint, ...] = ()
    interaction_questions: tuple[InteractionQuestion, ...] = ()
    pending: OnlineContextPending | None = None
    processed_graph_state: GraphState | None = None
    retrieved_graph_state: GraphState | None = None
    unresolved_choice_point_ids: tuple[str, ...] = ()
    run_id: str | None = None
    interaction_guidance: tuple[InteractionGuidance, ...] = ()
    downstream_guidance: DownstreamGuidance | None = None
    projection_trace: tuple[ProjectionDecision, ...] = ()
    diagnostics: OnlineContextDiagnostics = Field(
        default_factory=OnlineContextDiagnostics
    )

    @property
    def processed_retrieved_subgraph(self) -> GraphSpace | None:
        return (
            self.processed_graph_state.graph
            if self.processed_graph_state is not None
            else None
        )

    @property
    def processed_node_conflicts(self) -> tuple[NodeMergeConflict, ...]:
        """Derived historical view, not a separately stored overlay."""
        return (
            self.processed_graph_state.node_conflicts
            if self.processed_graph_state is not None
            else ()
        )

    @model_validator(mode="before")
    @classmethod
    def import_legacy_projection(cls, value):
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        graph = payload.pop("processed_retrieved_subgraph", None)
        conflicts = payload.pop("processed_node_conflicts", None)
        if graph is not None:
            imported = {"graph": graph}
            if conflicts is not None:
                imported["node_conflicts"] = conflicts
            state = GraphState.model_validate(imported)
            if (
                payload.get("processed_graph_state") is not None
                and GraphState.model_validate(payload["processed_graph_state"]) != state
            ):
                raise ValueError("legacy projection differs from processed_graph_state")
            payload["processed_graph_state"] = state
        elif conflicts:
            raise ValueError("legacy projected conflicts require their graph")
        return payload

    @property
    def relevant_graph(self) -> dict[str, Any] | None:
        state = self.processed_graph_state or self.retrieved_graph_state
        return export_inline_graph(state) if state is not None else None

    def public_payload(self) -> dict[str, Any]:
        """Public graphs always embed alternatives and conflict points on nodes."""
        payload = self.model_dump(mode="json")
        payload["relevant_graph"] = self.relevant_graph
        for key in (
            "processed_graph_state",
            "retrieved_graph_state",
        ):
            payload.pop(key, None)
        return payload

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        has_pending_interaction = (
            bool(self.interaction_questions) or self.pending is not None
        )
        if has_pending_interaction:
            if not self.interaction_questions or self.pending is None:
                raise ValueError(
                    "pending online context requires questions and pending state"
                )
            if self.status is not None:
                raise ValueError(
                    "pending online context must not contain a final status"
                )
            has_terminal_guidance = (
                self.processed_retrieved_subgraph is not None
                or self.downstream_guidance is not None
            )
            if has_terminal_guidance:
                raise ValueError(
                    "pending online context must not contain terminal guidance"
                )
            return self
        if self.status is None:
            raise ValueError("terminal online context requires a final status")
        if (
            self.status.startswith("completed")
            and self.processed_retrieved_subgraph is None
        ):
            raise ValueError(
                "completed online context requires a processed retrieved subgraph"
            )
        if self.status.startswith("completed") and self.downstream_guidance is None:
            raise ValueError("completed online context requires downstream guidance")
        if self.status in {"no_match", "unavailable"} and (
            self.processed_retrieved_subgraph is not None
            or self.downstream_guidance is not None
        ):
            raise ValueError(
                f"{self.status} online context must not contain terminal guidance"
            )
        return self


class OnlineContextEngine(Protocol):
    async def build(
        self,
        *,
        question: str,
        graph_state: GraphState,
        graph_state_fingerprint: str,
        dialect: str,
        initial_sql: str | None,
        column_context: tuple[ColumnSemanticContext, ...],
        question_budget: int | None = None,
        run_id: str | None = None,
    ) -> OnlineContextResult: ...

    async def resume(
        self,
        *,
        graph_state: GraphState,
        pending: OnlineContextPending,
        answers: tuple[InteractionAnswer, ...],
    ) -> OnlineContextResult: ...


class OnlineContextService:
    def __init__(
        self, engine: OnlineContextEngine, *, schema: SchemaDefinition
    ) -> None:
        self._engine = engine
        self._schema = schema

    async def run(
        self,
        *,
        question: str,
        graph_state: GraphState,
        dialect: str = "mysql",
        initial_sql: str | None = None,
        column_context: tuple[ColumnSemanticContext, ...] = (),
        question_budget: int | None = None,
        run_id: str | None = None,
    ) -> OnlineContextResult:
        if question_budget is not None and (
            isinstance(question_budget, bool)
            or not isinstance(question_budget, int)
            or question_budget < 0
        ):
            raise OnlineContextError("question budget must be a nonnegative integer")
        normalized = question.strip()
        if not normalized:
            raise OnlineContextError("online context question must not be empty")
        copied = GraphState.model_validate(graph_state.model_dump(mode="python"))
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self._schema).validate_state(copied)
        fingerprint = calculate_graph_state_fingerprint(copied)
        result = await self._engine.build(
            question=normalized,
            graph_state=copied,
            graph_state_fingerprint=fingerprint,
            dialect=dialect.strip() or "mysql",
            initial_sql=initial_sql.strip()
            if initial_sql and initial_sql.strip()
            else None,
            column_context=column_context,
            **({"run_id": run_id} if run_id is not None else {}),
            **(
                {"question_budget": question_budget}
                if question_budget is not None
                else {}
            ),
        )
        return self._validate_result(result, normalized, fingerprint)

    async def resume(
        self,
        *,
        graph_state: GraphState,
        pending: OnlineContextPending,
        answers: tuple[InteractionAnswer, ...],
    ) -> OnlineContextResult:
        copied = GraphState.model_validate(graph_state.model_dump(mode="python"))
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self._schema).validate_state(copied)
        fingerprint = calculate_graph_state_fingerprint(copied)
        if fingerprint != pending.graph_state_fingerprint:
            raise OnlineContextError(
                "pending graph state fingerprint does not match input"
            )
        _validate_answers(pending.questions, answers)
        result = await self._engine.resume(
            graph_state=copied,
            pending=pending,
            answers=answers,
        )
        return self._validate_result(result, pending.question, fingerprint)

    def _validate_result(
        self, result: OnlineContextResult, question: str, fingerprint: str
    ) -> OnlineContextResult:
        copied = OnlineContextResult.model_validate(result.model_dump(mode="python"))
        if copied.question != question or copied.graph_state_fingerprint != fingerprint:
            raise OnlineContextError(
                "online context result identity does not match request"
            )
        if copied.processed_retrieved_subgraph is not None:
            from cortex.application.ontology_graph import GraphManager

            GraphManager(self._schema).validate_state(copied.processed_graph_state)
        return copied


def calculate_graph_state_fingerprint(state: GraphState) -> str:
    canonical = json.dumps(
        state.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _validate_answers(
    questions: tuple[InteractionQuestion, ...], answers: tuple[InteractionAnswer, ...]
) -> None:
    by_id = {item.question_id: item for item in questions}
    supplied = {item.question_id: item for item in answers}
    if len(supplied) != len(answers) or set(supplied) != set(by_id):
        raise OnlineContextError(
            "answers must cover every interaction question exactly once"
        )
    for question_id, answer in supplied.items():
        question = by_id[question_id]
        allowed = {item.option_id for item in question.options}
        if not set(answer.selected_option_ids) <= allowed:
            raise OnlineContextError(
                f"answer contains an unknown option: {question_id}"
            )


__all__ = [
    "CandidateEvidenceAssessment",
    "ChoicePoint",
    "ColumnSemanticContext",
    "DownstreamGuidance",
    "InteractionAnswer",
    "InteractionGuidance",
    "InteractionOption",
    "InteractionQuestion",
    "OnlineContextDiagnostics",
    "OnlineContextEngine",
    "OnlineContextError",
    "OnlineContextPending",
    "OnlineContextResult",
    "OnlineContextService",
    "ProjectionDecision",
    "QueryExample",
    "RelevanceDecision",
    "RetrievalDocument",
    "RetrievedRepresentation",
    "calculate_graph_state_fingerprint",
]
