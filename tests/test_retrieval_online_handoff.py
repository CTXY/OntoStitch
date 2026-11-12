"""One actual retrieval path feeds both public graph output and user decisions."""

import asyncio
import json
from types import SimpleNamespace

from ontology_v2_fixtures import discussion_state

from cortex.application.online_context import (
    CandidateEvidenceAssessment,
    ChoicePoint,
    InteractionAnswer,
    InteractionOption,
    OnlineContextService,
)
from cortex.application.retrieval import RelevanceDecision
from cortex.application.retrieve_db import RetrieveDbService
from cortex.capabilities.online_context.engine import OnlineContextCapability
from cortex.capabilities.online_context.output.guidance import GuidanceSynthesis
from cortex.capabilities.retrieve_db.engine import RetrieveDbCapability
from cortex.infrastructure.artifacts.graph_html import write_graph_html_from_json
from cortex.infrastructure.retrieve.artifact import FileSystemRetrieveDbArtifactRecorder
from cortex.schema import load_root_schema


def test_conflict_survives_retrieval_until_user_selects_a_complete_definition(tmp_path):
    state = discussion_state()
    schema = load_root_schema()
    conflict = state.node_conflicts[0]

    class Selector:
        calls = 0

        async def select(self, *, representations, **kwargs):
            self.calls += 1
            return tuple(
                RelevanceDecision(
                    document_id=r.document.document_id,
                    status="relevant",
                    reason="Count-user knowledge applies",
                )
                for r in representations
            )

    class Organizer:
        async def organize(self, *, retrieved_graph_state, **kwargs):
            assert retrieved_graph_state.node_conflicts == (conflict,)
            options = tuple(
                InteractionOption(
                    option_id=a.alternative_id,
                    statement="Count each user once"
                    if i == 0
                    else "Count every user record",
                    candidate_sources=("memory_graph",),
                    graph_refs=(a.alternative_id,),
                    evidence_assessment=CandidateEvidenceAssessment(
                        source_types=("ontology_asset",),
                        applicability="applicable",
                        explanation="Historical count policy",
                    ),
                )
                for i, a in enumerate(conflict.alternatives)
            )
            return SimpleNamespace(
                choice_points=(
                    ChoicePoint(
                        choice_point_id="count-unit",
                        decision_question="How should repeated users be counted?",
                        scope="calculation",
                        options=options,
                    ),
                ),
                intra_node_assessments=(),
            )

    class Guidance:
        async def synthesize(self, **kwargs):
            return GuidanceSynthesis(
                guidance_content="Count each user once", interaction_guidance=()
            )

    async def run():
        selector = Selector()
        retrieval = RetrieveDbService(
            RetrieveDbCapability(
                relevance_selector=selector,
                ontology_schema=schema,
                checkpoint_root=tmp_path / "retrieval",
            ),
            schema=schema,
        )
        question = "How many users have qualifying posts?"
        initial_sql = "SELECT COUNT(users.Id) FROM users JOIN posts ON users.Id = posts.OwnerUserId"
        direct = await retrieval.run(
            question=question,
            graph_state=state,
            dialect="sqlite",
            initial_sql=initial_sql,
        )
        direct_node = next(
            n for n in direct.relevant_graph["nodes"] if n["id"] == conflict.node_id
        )
        assert len(direct_node["alternatives"]) == 2 and direct_node["conflict_points"]
        assert all(a["sources"] for a in direct_node["alternatives"])
        assert "node_conflicts" not in direct.public_payload()
        assert "subgraph" not in direct.public_payload()
        stored = direct.model_dump(mode="json")
        assert (
            "relevant_graph" not in stored
            and "subgraph" not in stored
            and "node_conflicts" not in stored
        )
        assert set(stored["retrieved_graph_state"]) == {"graph"}

        artifacts = FileSystemRetrieveDbArtifactRecorder(
            tmp_path / "artifacts", schema
        ).write(direct, trace_id="handoff")
        saved_graph = json.loads((artifacts / "graph.json").read_text())
        assert saved_graph == direct.relevant_graph
        assert "dp_count_method" in (artifacts / "graph.html").read_text()

        online = OnlineContextService(
            OnlineContextCapability(
                retrieval_service=retrieval,
                choice_organizer=Organizer(),
                guidance_synthesizer=Guidance(),
                ontology_schema=schema,
                checkpoint_path=tmp_path / "online.sqlite",
            ),
            schema=schema,
        )
        waiting = await online.run(
            question=question,
            graph_state=state,
            dialect="sqlite",
            initial_sql=initial_sql,
        )
        assert waiting.relevant_graph == direct.relevant_graph
        assert selector.calls == 2  # direct run and the one online retrieval call
        final = await online.resume(
            graph_state=state,
            pending=waiting.pending,
            answers=(
                InteractionAnswer(
                    question_id=waiting.interaction_questions[0].question_id,
                    selected_option_ids=(conflict.alternatives[0].alternative_id,),
                ),
            ),
        )
        node = next(
            n for n in final.relevant_graph["nodes"] if n["id"] == conflict.node_id
        )
        assert node["merge_status"] == "normal" and "alternatives" not in node
        assert "DISTINCT" in node["properties"]["action"]
        assert node["sources"] == direct_node["alternatives"][0]["sources"]
        assert final.projection_trace[0].removed_graph_refs == (
            conflict.alternatives[1].alternative_id,
        )
        assert selector.calls == 2  # resume never reruns retrieval
        assert len(state.node_conflicts[0].alternatives) == 2  # source remains intact
        via_graph = await retrieval.run(
            question=question,
            graph=state.graph,
            dialect="sqlite",
            initial_sql=initial_sql,
        )
        assert via_graph.relevant_graph == direct.relevant_graph
        from cortex.application.retrieve_db import RetrieveDbResult

        imported = RetrieveDbResult(
            status="completed",
            question=question,
            graph_fingerprint="legacy",
            subgraph=state.graph,
        )
        assert imported.retrieved_graph_state == state
        from cortex.application.online_context import (
            DownstreamGuidance,
            OnlineContextResult,
        )

        imported_online = OnlineContextResult(
            status="completed",
            question=question,
            graph_state_fingerprint="legacy",
            processed_retrieved_subgraph=state.graph,
            downstream_guidance=DownstreamGuidance(
                usage="reference", guidance_content=""
            ),
        )
        assert imported_online.processed_graph_state == state
        assert "processed_node_conflicts" not in imported_online.model_dump(mode="json")

    asyncio.run(run())


def test_html_reader_accepts_public_inline_domain_graph(tmp_path):
    source = tmp_path / "current_graph.json"
    source.write_text(discussion_state().model_dump_json())
    target = tmp_path / "graph.html"
    write_graph_html_from_json(
        source, target, load_root_schema(), title="Reviewed domain"
    )
    assert "dp_count_method" in target.read_text()


def test_portal_accepts_retrieval_result_envelope_without_another_graph_format():
    from cortex.schema import export_inline_graph
    from cortex.web.graphs import load_graph

    state = discussion_state()
    envelope = {
        "scenario": "db",
        "status": "completed",
        "question": "How many users?",
        "relevant_graph": export_inline_graph(state),
    }
    loaded = load_graph(envelope)
    assert loaded["state"] == state.model_dump(mode="json")
    assert "node_conflicts" not in loaded["state"]
    assert loaded["state"]["graph"]["nodes"][-1]["id"]
