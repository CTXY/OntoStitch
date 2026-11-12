"""Persistent online workflow behavior through its public service boundary."""

from types import SimpleNamespace

import pytest

from cortex.application.online_context import (
    CandidateEvidenceAssessment,
    ChoicePoint,
    InteractionAnswer,
    InteractionOption,
    OnlineContextError,
    OnlineContextService,
)
from cortex.application.retrieval import (
    RelevanceDecision,
    RetrievalDocument,
    RetrievedRepresentation,
)
from cortex.application.retrieve_db import RetrieveDbResult
from cortex.capabilities.online_context.engine import OnlineContextCapability
from cortex.capabilities.online_context.output.guidance import GuidanceSynthesis
from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema


def dependencies():
    state = GraphState(
        graph=GraphSpace(
            nodes=tuple(
                GraphNode(
                    id=f"table.{key}",
                    node_type="table",
                    name=key,
                    properties={"table_name": key},
                )
                for key in ("a", "b", "c", "d")
            )
        ),
        node_conflicts=(),
    )
    points = tuple(
        ChoicePoint(
            choice_point_id=f"point{index}",
            decision_question=f"Choose scope {index}",
            scope="output" if index == 0 else "filter",
            options=tuple(
                InteractionOption(
                    option_id=key,
                    statement=f"Use table {key}",
                    candidate_sources=("memory_graph",),
                    graph_refs=(f"table.{key}",),
                    evidence_assessment=CandidateEvidenceAssessment(
                        source_types=("ontology_asset",),
                        applicability="applicable",
                        explanation="Directly applicable definition",
                    ),
                )
                for key in keys
            ),
        )
        for index, keys in enumerate((("a", "b"), ("c", "d")))
    )
    representation = RetrievedRepresentation(
        document=RetrievalDocument(
            document_id="doc-a",
            canonical_node_id="table.a",
            node_type="table",
            representation_kind="canonical_node",
            searchable_text="a",
        ),
        score=1.0,
    )

    class Retrieval:
        calls = 0

        async def run(self, **kwargs):
            self.calls += 1
            return RetrieveDbResult(
                status="completed",
                question=kwargs["question"],
                graph_fingerprint="test",
                subgraph=state.graph,
                retrieved_graph_state=state,
                selected_node_ids=("table.a",),
                representations=(representation,),
                relevance=(
                    RelevanceDecision(
                        document_id="doc-a", status="relevant", reason="match"
                    ),
                ),
            )

    class Organizer:
        calls = 0

        async def organize(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(choice_points=points, intra_node_assessments=())

    class Guidance:
        calls = 0

        async def synthesize(self, **kwargs):
            self.calls += 1
            return GuidanceSynthesis(
                guidance_content="Apply user choices", interaction_guidance=()
            )

    return state, Retrieval, Organizer, Guidance


def service(path, budget=None):
    state, retrieval_type, organizer_type, guidance_type = dependencies()
    retrieval, organizer, guidance = retrieval_type(), organizer_type(), guidance_type()
    engine = OnlineContextCapability(
        retrieval_service=retrieval,
        choice_organizer=organizer,
        guidance_synthesizer=guidance,
        checkpoint_path=path,
        question_budget=budget,
        questions_per_round=1,
    )
    return (
        state,
        OnlineContextService(engine, schema=load_root_schema()),
        retrieval,
        guidance,
    )


def answer(result, option=None):
    question = result.interaction_questions[0]
    return (
        InteractionAnswer(
            question_id=question.question_id,
            selected_option_ids=(option or question.options[0].option_id,),
        ),
    )


def test_cross_engine_resume_preserves_retrieval_and_idempotency(tmp_path):
    import asyncio

    async def run():
        path = tmp_path / "online.sqlite"
        state, first, retrieval, _ = service(path)
        pending = await first.run(question="Select a scope", graph_state=state)
        assert pending.pending and pending.representations and pending.relevance
        first_answers = answer(pending)
        _, resumed, new_retrieval, guidance = service(path)
        second = await resumed.resume(
            graph_state=state, pending=pending.pending, answers=first_answers
        )
        assert second.pending and len(second.pending.prior_answers) == 1
        assert (
            second.representations == pending.representations
            and second.relevance == pending.relevance
        )
        assert retrieval.calls == 1 and new_retrieval.calls == 0
        duplicate = await resumed.resume(
            graph_state=state, pending=pending.pending, answers=first_answers
        )
        assert duplicate == second
        stale = (first_answers[0].model_copy(update={"selected_option_ids": ("b",)}),)
        with pytest.raises(OnlineContextError, match="stale"):
            await resumed.resume(
                graph_state=state, pending=pending.pending, answers=stale
            )
        final = await resumed.resume(
            graph_state=state, pending=second.pending, answers=answer(second)
        )
        assert final.status == "completed" and final.pending is None
        assert (
            final.representations == pending.representations
            and final.relevance == pending.relevance
        )
        assert guidance.calls == 1
        repeated = await resumed.resume(
            graph_state=state, pending=second.pending, answers=answer(second)
        )
        assert repeated == final and guidance.calls == 1
        public = final.public_payload()
        assert set(public["relevant_graph"]) == {"nodes", "edges"}

    asyncio.run(run())


def test_invalid_answer_does_not_consume_pending_round(tmp_path):
    import asyncio

    async def run():
        state, online, _, _ = service(tmp_path / "online.sqlite", budget=1)
        pending = await online.run(question="Select a scope", graph_state=state)
        with pytest.raises(OnlineContextError, match="unknown option"):
            await online.resume(
                graph_state=state,
                pending=pending.pending,
                answers=answer(pending, "invalid"),
            )
        final = await online.resume(
            graph_state=state, pending=pending.pending, answers=answer(pending)
        )
        assert final.status == "completed_with_unresolved"
        assert len(final.unresolved_choice_point_ids) == 1
        assert final.diagnostics.presented_questions == 1

    asyncio.run(run())


@pytest.mark.parametrize("budget", [0, 1])
def test_question_budget_never_defaults_unanswered_choices(tmp_path, budget):
    import asyncio

    async def run():
        state, online, _, _ = service(tmp_path / "online.sqlite", budget=budget)
        result = await online.run(question="Select a scope", graph_state=state)
        if budget:
            assert result.pending.remaining_budget == 0
            result = await online.resume(
                graph_state=state, pending=result.pending, answers=answer(result)
            )
        assert result.status == "completed_with_unresolved"
        assert len(result.unresolved_choice_point_ids) == 2 - budget
        assert result.diagnostics.presented_questions == budget

    asyncio.run(run())


def test_retry_after_guidance_failure_resumes_execution_instead_of_returning_old_pending(
    tmp_path,
):
    import asyncio

    async def run():
        path = tmp_path / "online.sqlite"
        state, online, _, guidance = service(path, budget=1)
        pending = await online.run(question="Select a scope", graph_state=state)
        supplied = answer(pending)

        async def fail(**kwargs):
            raise RuntimeError("temporary guidance failure")

        guidance.synthesize = fail
        with pytest.raises(RuntimeError, match="temporary guidance"):
            await online.resume(
                graph_state=state, pending=pending.pending, answers=supplied
            )
        _, rebuilt, retrieval, recovered_guidance = service(path, budget=1)
        recovered = await rebuilt.resume(
            graph_state=state, pending=pending.pending, answers=supplied
        )
        assert recovered.pending is None
        assert recovered.status == "completed_with_unresolved"
        assert retrieval.calls == 0 and recovered_guidance.calls == 1

    asyncio.run(run())
