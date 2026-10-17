from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

import sqlglot
from sqlglot import expressions

from cortex.application.online_context import (
    ColumnSemanticContext,
    InteractionAnswer,
    InteractionQuestion,
    OnlineContextDiagnostics,
    OnlineContextPending,
    OnlineContextResult,
    QueryExample,
)
from cortex.capabilities.online_context.discovery.models import ChoiceOrganizer
from cortex.capabilities.online_context.discovery.prioritization import (
    rank_choice_points,
    select_questions,
)
from cortex.capabilities.online_context.graph.context import (
    build_retrieved_graph_state,
    expand_context,
    without_untrusted_dimension_values,
)
from cortex.capabilities.online_context.output.guidance import GuidanceSynthesizer
from cortex.capabilities.online_context.output.projection import (
    build_downstream_guidance,
    project_graph,
)
from cortex.capabilities.online_context.output.query_examples import select_query_examples
from cortex.capabilities.online_context.retrieval.index import RepresentationIndex
from cortex.capabilities.online_context.retrieval.recall import TypedRecall
from cortex.capabilities.online_context.retrieval.selection import RelevanceSelector
from cortex.schema import GraphSpace, GraphState


class InitialSqlGenerator(Protocol):
    async def generate(self, *, question: str, schema_context: str, dialect: str) -> str: ...


class PhysicalSchemaRenderer(Protocol):
    def render(self, graph: GraphSpace) -> str: ...


class OnlineContextCapability:
    def __init__(
        self,
        *,
        relevance_selector: RelevanceSelector,
        choice_organizer: ChoiceOrganizer,
        guidance_synthesizer: GuidanceSynthesizer,
        initial_sql_generator: InitialSqlGenerator | None = None,
        schema_renderer: PhysicalSchemaRenderer | None = None,
        recall_limit: int = 80,
        typed_recall: TypedRecall | None = None,
        question_budget: int | None = None,
        query_example_index: Mapping[str, QueryExample] | None = None,
        query_example_top_k: int = 3,
    ) -> None:
        self._relevance_selector = relevance_selector
        self._choice_organizer = choice_organizer
        self._guidance_synthesizer = guidance_synthesizer
        self._initial_sql_generator = initial_sql_generator
        self._schema_renderer = schema_renderer
        self._recall_limit = recall_limit
        self._typed_recall = typed_recall
        # Validate configuration even if this request retrieves no conflict points.
        select_questions((), question_budget)
        self._question_budget = question_budget
        self._query_example_index = dict(query_example_index or {})
        self._query_example_top_k = query_example_top_k

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
    ) -> OnlineContextResult:
        budget = self._question_budget if question_budget is None else question_budget
        select_questions((), budget)
        graph_state = without_untrusted_dimension_values(graph_state)
        resolved_sql = _read_only_sql_or_none(initial_sql, dialect)
        if resolved_sql is None and self._initial_sql_generator is not None:
            if self._schema_renderer is None:
                raise ValueError("initial SQL generation requires a physical schema renderer")
            generated_sql = (
                await self._initial_sql_generator.generate(
                    question=question,
                    schema_context=self._schema_renderer.render(graph_state.graph),
                    dialect=dialect,
                )
            ).strip() or None
            resolved_sql = _read_only_sql_or_none(generated_sql, dialect)
        index = RepresentationIndex.build(graph_state, column_context=column_context)
        representations = (
            await self._typed_recall.recall(index, question, resolved_sql)
            if self._typed_recall
            else index.recall(
                question=question,
                initial_sql=resolved_sql,
                limit=self._recall_limit,
            )
        )
        if not representations:
            return OnlineContextResult(
                status="no_match",
                question=question,
                initial_sql=resolved_sql,
                graph_state_fingerprint=graph_state_fingerprint,
            )
        relevance = await self._relevance_selector.select(
            question=question,
            initial_sql=resolved_sql,
            representations=representations,
        )
        by_document = {item.document.document_id: item for item in representations}
        selected_node_ids = {
            by_document[item.document_id].document.canonical_node_id
            for item in relevance
            if item.status in {"relevant", "uncertain"} or _protected(by_document[item.document_id])
        }
        relevant_semantic_node_ids = {
            by_document[item.document_id].document.canonical_node_id
            for item in relevance
            if item.status == "relevant"
            and by_document[item.document_id].document.node_type
            in {"concept", "metric", "generation_rule"}
        }
        relevant_grounding_node_ids = {
            by_document[item.document_id].document.canonical_node_id
            for item in relevance
            if item.status == "relevant"
            and by_document[item.document_id].document.node_type == "dimension_value"
        }
        if not selected_node_ids:
            return OnlineContextResult(
                status="no_match",
                question=question,
                initial_sql=resolved_sql,
                graph_state_fingerprint=graph_state_fingerprint,
                representations=representations,
                relevance=relevance,
                diagnostics=_diagnostics(representations, relevance, ()),
            )
        context_node_ids = expand_context(graph_state, selected_node_ids)
        retrieved_state = build_retrieved_graph_state(graph_state, context_node_ids)
        organization = await self._choice_organizer.organize(
            question=question,
            initial_sql=resolved_sql,
            retrieved_graph_state=retrieved_state,
        )
        uncertain = tuple(
            item for item in organization.intra_node_assessments if item.status == "uncertain"
        )
        diagnostic_notes = tuple(item.reason for item in uncertain)
        choice_points = rank_choice_points(organization.choice_points)
        presented_points = select_questions(choice_points, budget)
        if presented_points:
            questions = _questions(presented_points)
            pending = OnlineContextPending(
                remaining_budget=None if budget is None else budget - len(questions),
                question=question,
                dialect=dialect,
                initial_sql=resolved_sql,
                graph_state_fingerprint=graph_state_fingerprint,
                selected_node_ids=tuple(sorted(context_node_ids)),
                relevant_semantic_node_ids=tuple(sorted(relevant_semantic_node_ids)),
                relevant_grounding_node_ids=tuple(sorted(relevant_grounding_node_ids)),
                choice_points=choice_points,
                questions=questions,
            )
            return OnlineContextResult(
                status=None,
                question=question,
                initial_sql=resolved_sql,
                graph_state_fingerprint=graph_state_fingerprint,
                retrieved_node_ids=tuple(sorted(context_node_ids)),
                retrieved_conflict_node_ids=_conflict_ids(retrieved_state),
                selected_node_ids=tuple(sorted(selected_node_ids)),
                representations=representations,
                relevance=relevance,
                choice_points=choice_points,
                interaction_questions=questions,
                pending=pending,
                diagnostics=_diagnostics(
                    representations,
                    relevance,
                    choice_points,
                    notes=diagnostic_notes,
                    presented_questions=len(questions),
                ),
            )
        return await self._terminal_from_selections(
            question=question,
            dialect=dialect,
            initial_sql=resolved_sql,
            fingerprint=graph_state_fingerprint,
            state=graph_state,
            selected_node_ids=context_node_ids,
            relevant_semantic_node_ids=relevant_semantic_node_ids,
            relevant_grounding_node_ids=relevant_grounding_node_ids,
            answers=(),
            choice_points=choice_points,
            selected_by_point={},
            explanations={},
            resolved_join_paths=(),
            representations=representations,
            relevance=relevance,
            diagnostic_notes=diagnostic_notes,
        )

    async def resume(
        self,
        *,
        graph_state: GraphState,
        pending: OnlineContextPending,
        answers: tuple[InteractionAnswer, ...],
    ) -> OnlineContextResult:
        graph_state = without_untrusted_dimension_values(graph_state)
        all_answers = (*pending.prior_answers, *answers)
        if pending.stage != "clarification":
            raise ValueError(
                "legacy staged JOIN pending state must be rebuilt with unified discovery"
            )
        selected_by_point, explanations = _selection_maps(pending.choice_points, all_answers)
        return await self._terminal_from_selections(
            question=pending.question,
            dialect=pending.dialect,
            initial_sql=pending.initial_sql,
            fingerprint=pending.graph_state_fingerprint,
            state=graph_state,
            selected_node_ids=set(pending.selected_node_ids),
            relevant_semantic_node_ids=set(pending.relevant_semantic_node_ids),
            relevant_grounding_node_ids=set(pending.relevant_grounding_node_ids),
            resolved_join_paths=pending.resolved_join_paths,
            choice_points=pending.choice_points,
            selected_by_point=selected_by_point,
            explanations=explanations,
            answers=all_answers,
            representations=(),
            relevance=(),
        )

    async def _terminal_from_selections(
        self,
        *,
        question,
        dialect,
        initial_sql,
        fingerprint,
        state,
        selected_node_ids,
        relevant_semantic_node_ids,
        relevant_grounding_node_ids=None,
        resolved_join_paths,
        choice_points,
        selected_by_point,
        explanations,
        answers,
        representations,
        relevance,
        diagnostic_notes=(),
    ) -> OnlineContextResult:
        relevant_grounding_node_ids = relevant_grounding_node_ids or set()
        synthesis = await self._guidance_synthesizer.synthesize(
            question=question,
            dialect=dialect,
            choice_points=tuple(
                point for point in choice_points if point.choice_point_id in selected_by_point
            ),
            answers=answers,
        )
        graph, trace = project_graph(
            state=state,
            selected_node_ids=selected_node_ids,
            choice_points=choice_points,
            selected_by_point=selected_by_point,
            explanations=explanations,
        )
        query_example_selection = select_query_examples(
            graph=state.graph,
            relevant_asset_ids=set(relevant_semantic_node_ids),
            example_index=self._query_example_index,
            top_k=self._query_example_top_k,
        )
        downstream_guidance = build_downstream_guidance(
            question=question,
            initial_sql=initial_sql,
            graph=graph,
            relevant_semantic_node_ids=relevant_semantic_node_ids,
            relevant_grounding_node_ids=relevant_grounding_node_ids,
            excluded_alternative_ids={ref for item in trace for ref in item.removed_graph_refs},
            interaction_guidance=synthesis.interaction_guidance,
            resolved_guidance_content=synthesis.guidance_content,
            resolved_join_paths=tuple(resolved_join_paths),
            query_examples=query_example_selection.examples,
            conflicts=state.node_conflicts,
        )
        query_example_notes = (
            (
                "missing query example sources: "
                + ", ".join(query_example_selection.missing_source_ids),
            )
            if query_example_selection.missing_source_ids
            else ()
        )
        custom = bool(explanations)
        return OnlineContextResult(
            status="completed_with_custom_guidance" if custom else "completed",
            question=question,
            initial_sql=initial_sql,
            graph_state_fingerprint=fingerprint,
            retrieved_node_ids=tuple(sorted(selected_node_ids)),
            retrieved_conflict_node_ids=_conflict_ids(
                build_retrieved_graph_state(state, set(selected_node_ids))
            ),
            selected_node_ids=tuple(sorted(selected_node_ids)),
            relevant_semantic_node_ids=tuple(sorted(relevant_semantic_node_ids)),
            relevant_grounding_node_ids=tuple(sorted(relevant_grounding_node_ids)),
            representations=representations,
            relevance=relevance,
            choice_points=choice_points,
            processed_retrieved_subgraph=graph,
            interaction_guidance=synthesis.interaction_guidance,
            downstream_guidance=downstream_guidance,
            projection_trace=trace,
            diagnostics=_diagnostics(
                representations,
                relevance,
                choice_points,
                notes=tuple(diagnostic_notes) + query_example_notes,
                presented_questions=len(answers),
            ),
        )


def _questions(points) -> tuple[InteractionQuestion, ...]:
    return tuple(
        InteractionQuestion(
            question_id=f"question:{point.choice_point_id}",
            choice_point_ids=(point.choice_point_id,),
            scope=point.scope,
            prompt=point.decision_question,
            selection_mode=point.selection_mode,
            options=point.options,
            evidence_priority=point.evidence_priority,
            evidence_priority_reason=point.evidence_priority_reason,
            source_category=point.source_category,
            applicable_candidate_count=point.applicable_candidate_count,
            candidate_relevance_sum=point.candidate_relevance_sum,
            priority_score=point.priority_score,
            priority_reason=point.priority_reason,
        )
        for point in points
    )


def _read_only_sql_or_none(sql: str | None, dialect: str) -> str | None:
    text = (sql or "").strip()
    if not text:
        return None
    try:
        parsed = sqlglot.parse(text, read=dialect or None)
    except (sqlglot.errors.ParseError, ValueError):
        return None
    if len(parsed) != 1:
        return None
    tree = parsed[0]
    if not isinstance(tree, (expressions.Select, expressions.Union)):
        return None
    return text


def _selection_maps(choice_points, answers):
    by_question = {answer.question_id: answer for answer in answers}
    selected_by_point = {}
    explanations = {}
    for point in choice_points:
        answer = by_question.get(f"question:{point.choice_point_id}")
        if answer is None:
            continue
        selected_by_point[point.choice_point_id] = answer.selected_option_ids
        if not answer.selected_option_ids and answer.explanation.strip():
            explanations[point.choice_point_id] = answer.explanation.strip()
    return selected_by_point, explanations


def _protected(item) -> bool:
    return bool({"question_exact", "initial_sql_exact"} & set(item.channels))


def _conflict_ids(state: GraphState) -> tuple[str, ...]:
    return tuple(sorted(item.node_id for item in state.node_conflicts))


def _diagnostics(representations, relevance, points, *, notes=(), presented_questions=0):
    return OnlineContextDiagnostics(
        recalled_documents=len(representations),
        relevant_documents=sum(item.status == "relevant" for item in relevance),
        uncertain_documents=sum(item.status == "uncertain" for item in relevance),
        conflict_points=len(points),
        presented_questions=presented_questions,
        prioritization_policy="source_score_v2",
        notes=notes,
    )


__all__ = ["OnlineContextCapability"]
