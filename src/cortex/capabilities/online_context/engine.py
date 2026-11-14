"""Interaction and guidance over the single retrieve-db retrieval snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cortex.application.online_context import (
    ChoicePoint,
    ClarificationFilterDecision,
    InteractionAnswer,
    InteractionQuestion,
    OnlineContextDiagnostics,
    OnlineContextPending,
    OnlineContextResult,
    QueryExample,
)
from cortex.application.retrieve_db import RetrieveDbService
from cortex.capabilities.online_context.discovery.prioritization import (
    rank_choice_points,
    select_questions,
)
from cortex.capabilities.online_context.llm.prompts import prompt_version
from cortex.capabilities.online_context.output.guidance import GuidanceAgentOutput
from cortex.capabilities.online_context.output.projection import (
    build_downstream_guidance,
    project_graph_state,
)
from cortex.capabilities.online_context.output.query_examples import (
    select_query_examples,
)
from cortex.capabilities.online_context.workflow.runtime import OnlineWorkflow
from cortex.capabilities.retrieve_db.graph.context import build_retrieved_graph_state
from cortex.schema import GraphState, calculate_schema_hash, load_root_schema


class OnlineContextCapability:
    def __init__(
        self,
        *,
        retrieval_service: RetrieveDbService,
        choice_organizer,
        guidance_synthesizer,
        clarification_filter=None,
        question_budget=None,
        questions_per_round=1,
        checkpoint_path="workspace/checkpoints/online_context.sqlite",
        context_settings=None,
        query_example_index: Mapping[str, QueryExample] | None = None,
        query_example_top_k=3,
        ontology_schema=None,
        on_retrieved=None,
    ):
        self._retrieval = retrieval_service
        self._on_retrieved = on_retrieved
        self._ontology_schema = ontology_schema or load_root_schema()
        self._choice_organizer = choice_organizer
        self._guidance_synthesizer = guidance_synthesizer
        self._clarification_filter = clarification_filter
        select_questions((), question_budget)
        if (
            isinstance(questions_per_round, bool)
            or not isinstance(questions_per_round, int)
            or questions_per_round < 1
        ):
            raise ValueError("questions_per_round must be a positive integer")
        self._question_budget = question_budget
        self._questions_per_round = questions_per_round
        self._query_example_index = dict(query_example_index or {})
        self._query_example_top_k = query_example_top_k
        self._workflow = OnlineWorkflow(
            self,
            Path(checkpoint_path),
            {
                "prompts": prompt_version(),
                "sql_handoff": "resolved_constraints_v1",
                "schemas": {
                    "choice": ChoicePoint.model_json_schema(),
                    "answer": InteractionAnswer.model_json_schema(),
                    "guidance": GuidanceAgentOutput.model_json_schema(),
                },
                "schema": calculate_schema_hash(self._ontology_schema),
                "questions_per_round": questions_per_round,
                "settings": context_settings or {},
                "query_examples": {
                    k: v.model_dump(mode="json")
                    for k, v in self._query_example_index.items()
                },
                "query_example_top_k": query_example_top_k,
                "retrieval_context": getattr(
                    retrieval_service, "context_fingerprint", None
                ),
                "clarification_filter": getattr(
                    clarification_filter, "context_fingerprint", None
                ),
                "filter_schema": ClarificationFilterDecision.model_json_schema(),
            },
        )

    async def build(
        self,
        *,
        question,
        graph_state,
        graph_state_fingerprint,
        dialect,
        initial_sql,
        column_context,
        question_budget=None,
        run_id=None,
    ):
        budget = self._question_budget if question_budget is None else question_budget
        select_questions((), budget)
        return await self._workflow.start(
            {
                "question": question,
                "graph_state": graph_state.model_dump(mode="json"),
                "graph_state_fingerprint": graph_state_fingerprint,
                "dialect": dialect,
                "initial_sql": initial_sql,
                "column_context": [c.model_dump(mode="json") for c in column_context],
                "question_budget": budget,
            },
            run_id=run_id,
        )

    async def resume(self, *, graph_state, pending, answers):
        return await self._workflow.resume(graph_state, pending, answers)

    async def _retrieve(self, request, run_id):
        from cortex.application.retrieval import ColumnSemanticContext

        result = await self._retrieval.run(
            question=request["question"],
            graph_state=GraphState.model_validate(request["graph_state"]),
            dialect=request["dialect"],
            initial_sql=request["initial_sql"],
            column_context=tuple(
                ColumnSemanticContext.model_validate(c)
                for c in request["column_context"]
            ),
            run_id="online-retrieval-" + run_id,
        )

        if self._on_retrieved is not None:
            self._on_retrieved(result)
        return result

    async def _discover(self, request, retrieval):
        organization = await self._choice_organizer.organize(
            question=request["question"],
            initial_sql=retrieval.initial_sql,
            retrieved_graph_state=retrieval.retrieved_graph_state,
        )
        points = organization.choice_points
        notes = tuple(
            a.reason
            for a in organization.intra_node_assessments
            if a.status == "uncertain"
        )
        return points, notes

    async def _filter(self, request, retrieval, points):
        from cortex.capabilities.online_context.filtering import (
            ClarificationFilterResult,
        )

        if self._clarification_filter is None or not points:
            return ClarificationFilterResult(choice_points=rank_choice_points(points))
        result = await self._clarification_filter.filter(
            question=request["question"],
            initial_sql=retrieval.initial_sql,
            retrieved_graph_state=retrieval.retrieved_graph_state,
            choice_points=points,
            dialect=request["dialect"],
        )
        # The source-based scoring formula and all existing budgets are unchanged.
        return result.model_copy(
            update={"choice_points": rank_choice_points(result.choice_points)}
        )

    def _pending_result(
        self,
        request,
        retrieval,
        points,
        questions,
        answers,
        remaining_budget,
        run_id,
        identity,
        notes,
        automatic_guidance=(),
        filter_decisions=(),
    ):
        state = retrieval.retrieved_graph_state
        pending = OnlineContextPending(
            run_id=run_id,
            workflow_fingerprint=identity,
            question=request["question"],
            dialect=request["dialect"],
            initial_sql=retrieval.initial_sql,
            graph_state_fingerprint=request["graph_state_fingerprint"],
            remaining_budget=remaining_budget,
            selected_node_ids=tuple(n.id for n in state.graph.nodes),
            relevant_semantic_node_ids=retrieval.relevant_semantic_node_ids,
            relevant_grounding_node_ids=retrieval.relevant_grounding_node_ids,
            prior_answers=answers,
            choice_points=points,
            questions=questions,
        )
        return OnlineContextResult(
            status=None,
            run_id=run_id,
            question=request["question"],
            initial_sql=retrieval.initial_sql,
            graph_state_fingerprint=request["graph_state_fingerprint"],
            retrieved_node_ids=tuple(n.id for n in state.graph.nodes),
            retrieved_conflict_node_ids=_conflict_ids(state),
            selected_node_ids=retrieval.selected_node_ids,
            relevant_semantic_node_ids=retrieval.relevant_semantic_node_ids,
            relevant_grounding_node_ids=retrieval.relevant_grounding_node_ids,
            representations=retrieval.representations,
            relevance=retrieval.relevance,
            retrieved_graph_state=state,
            choice_points=points,
            interaction_questions=questions,
            pending=pending,
            interaction_guidance=automatic_guidance,
            filter_decisions=filter_decisions,
            diagnostics=_diagnostics(
                retrieval.representations,
                retrieval.relevance,
                points,
                notes=notes,
                presented_questions=len(answers) + len(questions),
            ),
        )

    async def _finish(
        self,
        request,
        retrieval,
        points,
        answers,
        notes,
        automatic_guidance=(),
        filter_decisions=(),
    ):
        if retrieval.retrieved_graph_state is None:
            return OnlineContextResult(
                status="no_match",
                question=request["question"],
                initial_sql=retrieval.initial_sql,
                graph_state_fingerprint=request["graph_state_fingerprint"],
                representations=retrieval.representations,
                relevance=retrieval.relevance,
            )
        selected, explanations = _selection_maps(points, answers)
        state = retrieval.retrieved_graph_state
        return await self._terminal_from_selections(
            question=request["question"],
            dialect=request["dialect"],
            initial_sql=retrieval.initial_sql,
            fingerprint=request["graph_state_fingerprint"],
            state=state,
            selected_node_ids={n.id for n in state.graph.nodes},
            relevant_semantic_node_ids=set(retrieval.relevant_semantic_node_ids),
            relevant_grounding_node_ids=set(retrieval.relevant_grounding_node_ids),
            resolved_join_paths=(),
            choice_points=points,
            selected_by_point=selected,
            explanations=explanations,
            answers=answers,
            representations=retrieval.representations,
            relevance=retrieval.relevance,
            diagnostic_notes=notes,
            automatic_guidance=automatic_guidance,
            filter_decisions=filter_decisions,
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
        automatic_guidance=(),
        filter_decisions=(),
    ) -> OnlineContextResult:
        relevant_grounding_node_ids = relevant_grounding_node_ids or set()
        synthesis = await self._guidance_synthesizer.synthesize(
            question=question,
            dialect=dialect,
            choice_points=tuple(
                point
                for point in choice_points
                if point.choice_point_id in selected_by_point
            ),
            answers=answers,
        )
        combined_guidance = (*synthesis.interaction_guidance, *automatic_guidance)
        resolved_content = "\n".join(
            text
            for text in (
                synthesis.guidance_content,
                *(item.should_be for item in automatic_guidance),
            )
            if text.strip()
        )
        projected, trace = project_graph_state(
            schema=self._ontology_schema,
            state=state,
            selected_node_ids=selected_node_ids,
            choice_points=choice_points,
            selected_by_point=selected_by_point,
            explanations=explanations,
        )
        graph = projected.graph
        query_example_selection = select_query_examples(
            graph=state.graph,
            relevant_asset_ids=set(relevant_semantic_node_ids),
            example_index=self._query_example_index,
            top_k=self._query_example_top_k,
        )
        from cortex.capabilities.online_context.output.projection import (
            guidance_selection_refs,
        )

        confirmed_refs, unselected_refs = guidance_selection_refs(
            choice_points, selected_by_point
        )
        downstream_guidance = build_downstream_guidance(
            question=question,
            initial_sql=initial_sql,
            graph=graph,
            confirmed_graph_refs=confirmed_refs,
            unselected_graph_refs=unselected_refs,
            relevant_semantic_node_ids=relevant_semantic_node_ids,
            relevant_grounding_node_ids=relevant_grounding_node_ids,
            excluded_alternative_ids={
                ref for item in trace for ref in item.removed_graph_refs
            },
            interaction_guidance=combined_guidance,
            resolved_guidance_content=resolved_content,
            resolved_join_paths=tuple(resolved_join_paths),
            query_examples=query_example_selection.examples,
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
        unresolved = tuple(
            p.choice_point_id
            for p in choice_points
            if p.choice_point_id not in selected_by_point
        )
        return OnlineContextResult(
            status="completed_with_unresolved"
            if unresolved
            else ("completed_with_custom_guidance" if custom else "completed"),
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
            processed_graph_state=projected,
            retrieved_graph_state=state,
            unresolved_choice_point_ids=unresolved,
            interaction_guidance=combined_guidance,
            filter_decisions=filter_decisions,
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


def _conflict_ids(state: GraphState) -> tuple[str, ...]:
    return tuple(sorted(item.id for item in state.conflict_nodes))


def _diagnostics(
    representations, relevance, points, *, notes=(), presented_questions=0
):
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
