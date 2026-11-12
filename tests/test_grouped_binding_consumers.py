"""New grouped bindings survive retrieval and authored business-edge projection."""

import pytest

from cortex.capabilities.online_context.discovery.evidence import (
    _analysis_graph_payload,
    _graph_ids,
)
from cortex.capabilities.online_context.output.projection import (
    asset_summary,
    project_graph,
    semantic_asset_payload,
)
from cortex.capabilities.retrieve_db.retrieval.index import RepresentationIndex
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, load_root_schema
from cortex.schema.bindings import select_target_groups, target_groups


def school_state():
    school = GraphNode(
        id="concept.school",
        node_type="concept",
        name="School",
        properties={
            "targets": [["column.schools.CDSCode"], ["column.frpm.CDSCode"]],
            "target_expressions": {"1": "frpm.CDSCode"},
        },
    )
    opening_date = GraphNode(
        id="concept.opening_date",
        node_type="concept",
        name="Opening date",
        properties={"targets": [["column.schools.OpenDate"]]},
    )
    opening = GraphNode(
        id="event.school_opening",
        node_type="event",
        name="School opening",
        description="A school being opened at its recorded opening date.",
        properties={"targets": []},
    )
    edges = (
        GraphEdge(
            id="opens",
            edge_type="opens",
            from_node_id=opening.id,
            to_node_id=school.id,
            properties={"description": "School is opened."},
        ),
        GraphEdge(
            id="time",
            edge_type="occurred_at_date",
            from_node_id=opening.id,
            to_node_id=opening_date.id,
            properties={"description": "Date of opening."},
        ),
    )
    return GraphState(
        graph=GraphSpace(nodes=(school, opening_date, opening), edges=edges)
    )


def test_discovery_keeps_stored_groups_and_separate_read_only_match_views():
    state = school_state()
    payload = _analysis_graph_payload(state)
    school = payload["graph"]["nodes"][0]
    assert school["properties"]["targets"] == state.graph.nodes[0].properties["targets"]
    assert school["match_views"][1]["expression"] == "frpm.CDSCode"
    assert "target:concept.school:2" in _graph_ids(state)


def test_retrieval_indexes_both_complete_match_groups():
    index = RepresentationIndex.build(school_state())
    targets = [d for d in index.documents if d.representation_kind == "concept_target"]
    assert len(targets) == 3
    school_targets = [d for d in targets if d.canonical_node_id == "concept.school"]
    assert school_targets[1].structured_signals["target"]["physical_ids"] == [
        "column.frpm.CDSCode"
    ]
    assert "frpm.CDSCode" in school_targets[1].searchable_text


def test_projecting_event_retains_saved_custom_participant_and_time_edges():
    state = school_state()
    projected, _ = project_graph(
        schema=load_root_schema(),
        state=state,
        selected_node_ids={"event.school_opening"},
        choice_points=(),
        selected_by_point={},
    )
    assert {n.id for n in projected.nodes} == {n.id for n in state.graph.nodes}
    assert projected.edges == state.graph.edges
    event = next(n for n in projected.nodes if n.node_type == "event")
    payload = semantic_asset_payload(event, relations=projected.edges)
    assert payload["type"] == "event"
    assert "School opening" in asset_summary(payload, chinese=False)


def test_selecting_groups_reindexes_expressions_without_legacy_target_records():
    original = {
        "targets": [["column.a.x"], ["column.b.y", "column.b.z"]],
        "target_expressions": {"0": "a.x", "1": "b.y BETWEEN 1 AND b.z"},
    }
    selected = select_target_groups(original, [1])
    assert selected["targets"] == [["column.b.y", "column.b.z"]]
    assert selected["target_expressions"] == {"0": "b.y BETWEEN 1 AND b.z"}
    assert original["target_expressions"]["1"] == "b.y BETWEEN 1 AND b.z"


def test_grouped_target_reader_rejects_legacy_records_instead_of_converting():
    with pytest.raises(ValueError, match="nonempty lists"):
        target_groups({"targets": [{"id": "old", "references": ["column.t.x"]}]})


def matching_catalog(group, expression):
    table = GraphNode(
        id="table.t", node_type="table", name="t", properties={"table_name": "t"}
    )
    columns = tuple(
        GraphNode(
            id=f"column.t.{name}",
            node_type="column",
            name=name,
            properties={"table_ref": table.id, "column_name": name},
        )
        for name in ("a", "b")
    )
    value = GraphNode(
        id="dimension_value.t.one",
        node_type="dimension_value",
        name="One",
        properties={"column_ref": columns[0].id, "value": 1},
    )
    concept = GraphNode(
        id="concept.match",
        node_type="concept",
        name="Matched object",
        properties={"targets": [group], "target_expressions": {"0": expression}},
    )
    return GraphState(graph=GraphSpace(nodes=(table, *columns, value, concept)))


def test_formal_binding_expression_cannot_use_another_known_catalog_field():
    from cortex.application.ontology_graph import GraphManager, GraphRejected

    with pytest.raises(GraphRejected, match="outside its target group"):
        GraphManager(load_root_schema()).prepare_state(
            matching_catalog(["column.t.a"], "t.b = 1")
        )


@pytest.mark.parametrize(
    "group,expression",
    [
        (["dimension_value.t.one"], "t.a = 1"),
        (["table.t"], "t.a < t.b"),
    ],
)
def test_value_and_table_matches_allow_their_catalog_columns(group, expression):
    from cortex.application.ontology_graph import GraphManager

    prepared, _ = GraphManager(load_root_schema()).prepare_state(
        matching_catalog(group, expression)
    )
    assert prepared.graph.nodes
