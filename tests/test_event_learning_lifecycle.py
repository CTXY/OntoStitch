"""Optional Events survive authoring, retention and the training retrieval boundary."""

import asyncio
from typing import ClassVar

from test_agent_runtime import ScriptModel, call, message, session_for
from test_unified_retrieval_workflow import service

from cortex.application.retrieval import RetrievedRepresentation
from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph
from cortex.capabilities.sql_training.learning.runtime import LearningAgent
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.schema import GraphSpace, GraphState


def occurrence():
    return {
        "id": "event.order_placement", "node_type": "event", "name": "Order placement",
        "description": "The occurrence of placing an order, with an Order participant and a placement context represented by the source records.",
        "properties": {"targets": []},
    }


def test_event_only_episode_needs_no_three_type_quota(case_graph):
    session = session_for(case_graph)
    session.record["question"] = "Describe order placement."
    model = ScriptModel([
        message(call("edit_graph", {"edit_id": "event", "expected_revision": session.graph.working_revision,
                                     "upserts": [occurrence()]})),
        message(call("handoff", {"outcome": "ready_for_validation", "direct_type_coverage": ["event"]})),
    ])
    result = LearningAgent(model).run(session)
    assert result.direct_completed
    assert [a.node_type.value for a in session.graph.working.semantic_assets] == ["event"]
    assert not session.graph.accepted.semantic_assets


def test_event_and_role_relation_survive_selection_and_case_namespace(case_graph):
    session = session_for(case_graph)
    before = session.graph.accepted
    tool = next(t for t in build_tools(session) if t.name == "edit_graph")
    result = tool.invoke({"edit_id": "roles", "expected_revision": before.revision,
                         "relation_contracts": [{"id": "places_order", "name": "Places order",
                             "description": "An order placement occurrence points to the business object placed.",
                             "from_node_types": ["event"], "to_node_types": ["concept"],
                             "direction_semantics": "Placement occurrence toward its order object.",
                             "evidence_requirement": "The order and occurrence are supported as belonging together."}],
                         "upserts": [occurrence(), {"id": "concept.order", "node_type": "concept",
                                      "name": "Order", "description": "An order placed by a customer.",
                                      "properties": {"targets": [["column.orders.id"]]}}],
                         "edge_operations": [{"operation": "create_edge", "edge": {
                             "id": "edge.order_role", "edge_type": "places_order",
                             "from_node_id": "event.order_placement", "to_node_id": "concept.order",
                             "properties": {"description": "Order is the object placed by this occurrence."}}}]})
    assert result["status"] == "ok", result
    trial = session.graph.working
    retained = session.graph.select(assessed_revision=trial.revision, accepted_before=before.revision,
                                    member_ids=["event.order_placement", "concept.order", "edge.order_role"])
    session.graph.restore(retained.revision, retained.revision)
    incoming = build_incoming_graph(session.graph.schema, assets=before.physical_assets,
                                   cases=[("case", retained.semantic_assets, retained.explicit_edges)],
                                   relation_contracts=retained.graph.relation_contracts,
                                   existing=GraphSpace())
    by_id = {n.id: n for n in incoming.graph.nodes}
    edge = next(e for e in incoming.graph.edges if e.edge_type == "places_order")
    assert by_id[edge.from_node_id].node_type == "event"
    assert by_id[edge.to_node_id].node_type == "concept"
    assert edge.properties["description"] == "Order is the object placed by this occurrence."
    assert "role" not in edge.properties and "qualifier" not in edge.properties
    assert edge.sources[0].case_id == "case"
    assert incoming.graph.relation_contracts == retained.graph.relation_contracts


def test_training_retrieval_accepts_event_as_selected_semantic_asset(tmp_path, case_graph):
    session = session_for(case_graph)
    tool = next(t for t in build_tools(session) if t.name == "edit_graph")
    assert tool.invoke({"edit_id": "event", "expected_revision": session.graph.working_revision,
                        "upserts": [occurrence()]})["status"] == "ok"
    state = GraphState(graph=session.graph.working.graph)

    class EventRecall:
        limits: ClassVar = {"event": 12}

        async def recall(self, index, question, sql):
            return tuple(RetrievedRepresentation(document=d, score=1, channels=("question_exact",))
                         for d in index.documents if d.canonical_node_id == "event.order_placement")

    result = asyncio.run(service(tmp_path / "retrieve", EventRecall()).run(
        question="Order placement", graph_state=state))
    assert result.status == "completed"
    assert result.relevant_ontology_asset_node_ids == ("event.order_placement",)
    assert result.relevant_semantic_node_ids == ("event.order_placement",)
    assert result.subgraph.nodes[0].node_type == "event"


def test_new_write_rejection_leaves_a_chance_to_correct_schema(case_graph):
    session = session_for(case_graph)
    session.no_progress = 2
    invalid = occurrence()
    invalid["properties"] = {"unsupported_field": True}
    model = ScriptModel([
        message(call("edit_graph", {"edit_id": "invalid", "expected_revision": session.graph.working_revision,
                                     "upserts": [invalid]})),
        message(call("edit_graph", {"edit_id": "corrected", "expected_revision": session.graph.working_revision,
                                     "upserts": [occurrence()]})),
        message(call("handoff", {"outcome": "ready_for_validation"})),
    ])
    result = LearningAgent(model).run(session)
    assert result.direct_completed
    assert result.model_calls == 3
    assert len(session.observed_rejections) == 1
    assert session.graph.working.semantic_assets[0].id == "event.order_placement"


def test_misspelled_read_arguments_are_not_silently_ignored(case_graph):
    import pytest
    from pydantic import ValidationError

    tool = next(t for t in build_tools(session_for(case_graph)) if t.name == "inspect_graph")
    with pytest.raises(ValidationError, match="Extra inputs"):
        tool.invoke({"guide": True})


def test_event_ids_survive_public_training_report_conversion():
    from cortex.capabilities.sql_training.engine import _case_result

    report = _case_result({"source_id": "event-case", "correct": True,
                           "accepted_event_ids": ["event.order_placement"]}, None)
    assert report.accepted_event_ids == ("event.order_placement",)
    assert report.model_dump(mode="json")["accepted_event_ids"] == ["event.order_placement"]
