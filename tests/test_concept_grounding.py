"""Match groups and explicit connections are independent, validated contracts."""

import pytest
from pydantic import ValidationError

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.online_context.output.projection import project_graph
from cortex.capabilities.sql_training.models.semantic_mapping import ConceptProperties
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, load_root_schema


def node(key, kind, properties):
    return GraphNode(id=key, name=key, node_type=kind, properties=properties)


def edge(key, source, target, kind="grounded_on", description="Explicit business connection."):
    return GraphEdge(
        id=key, edge_type=kind, from_node_id=source, to_node_id=target,
        properties={"description": description},
    )


@pytest.fixture
def state():
    nodes = (
        node("table", "table", {"table_name": "customers"}),
        node("status", "column", {"table_ref": "table", "column_name": "status", "data_type": "TEXT"}),
        node("amount", "column", {"table_ref": "table", "column_name": "amount", "data_type": "REAL"}),
        node("active", "dimension_value", {"column_ref": "status", "value": "active"}),
        node("concept", "concept", {
            "definition": "Active customer records, with the exact state value bound.",
            "targets": [["active"]],
        }),
    )
    return GraphManager(load_root_schema()).prepare_state(
        GraphState(graph=GraphSpace(nodes=nodes, edges=(edge("active_binding", "concept", "active"),)))
    )[0]


def test_nested_matching_groups_and_no_legacy_conversion():
    value = ConceptProperties(targets=[["column.customers.amount", "column.customers.status"]])
    assert value.targets == [["column.customers.amount", "column.customers.status"]]
    for targets in [[], [[]], [{"id": "old", "sql_expression": "customers.amount", "references": ["column.customers.amount"]}]]:
        with pytest.raises(ValidationError):
            ConceptProperties(targets=targets)


def test_matching_expressions_preserve_literal_case():
    upper = ConceptProperties(targets=[["column.customers.status"]], target_expressions={"0": "customers.status = 'A'"})
    lower = ConceptProperties(targets=[["column.customers.status"]], target_expressions={"0": "customers.status = 'a'"})
    assert upper != lower
    with pytest.raises(ValidationError, match="duplicate matching"):
        ConceptProperties(targets=[["column.customers.status"], ["column.customers.status"]])


def test_matching_groups_do_not_generate_connections(state):
    concept = next(n for n in state.graph.nodes if n.id == "concept")
    candidate = concept.model_copy(deep=True)
    candidate.properties["targets"] = [["active", "amount"]]
    manager = GraphManager(load_root_schema(), state)
    result = manager.apply(
        [{"operation": "update_node", "node": candidate.model_dump(mode="json")}],
        expected_revision=manager.revision, request_id="match-input",
    )
    assert result.status == "applied", result.errors
    assert {e.to_node_id for e in manager.get_edges("concept")} == {"active"}


def test_unknown_matching_input_rejects_edit_atomically(state):
    manager = GraphManager(load_root_schema(), state)
    before = manager.get_snapshot()
    owner = manager.get_node("concept").model_copy(deep=True)
    owner.properties["targets"] = [["active", "missing"]]
    result = manager.apply(
        [{"operation": "update_node", "node": owner.model_dump(mode="json")}],
        expected_revision=manager.revision, request_id="unknown-match",
    )
    assert result.status == "rejected"
    assert manager.get_snapshot() == before


def test_one_concept_can_connect_multiple_physical_fields(state):
    manager = GraphManager(load_root_schema(), state)
    owner = manager.get_node("concept").model_copy(deep=True)
    owner.properties["targets"] = [["active", "amount"]]
    result = manager.apply([
        {"operation": "update_node", "node": owner.model_dump(mode="json")},
        {"operation": "create_edge", "edge": edge("amount_binding", "concept", "amount").model_dump(mode="json")}
    ], expected_revision=manager.revision, request_id="multiple-physical")
    assert result.status == "applied", result.errors
    assert {e.to_node_id for e in manager.get_edges("concept")} == {"active", "amount"}


def test_overlapping_targets_do_not_imply_duplicate_direct_connections(state):
    manager = GraphManager(load_root_schema(), state)
    other = node("other", "concept", {"definition": "Another evidenced interpretation.", "targets": [["active"]]})
    result = manager.apply([
        {"operation": "create_node", "node": other.model_dump(mode="json")},
        {"operation": "create_edge", "edge": edge("business", "other", "concept", "is_a").model_dump(mode="json")},
    ], expected_revision=manager.revision, request_id="overlap")
    assert result.status == "applied", result.errors
    assert not any(e.edge_type == "grounded_on" and e.from_node_id == "other" for e in manager.get_snapshot().graph.edges)


def test_second_direct_concept_connection_to_same_endpoint_is_rejected(state):
    manager = GraphManager(load_root_schema(), state)
    before = manager.get_snapshot()
    result = manager.apply([
        {"operation": "create_node", "node": node("other", "concept", {"targets": [["active"]]}).model_dump(mode="json")},
        {"operation": "create_edge", "edge": edge("duplicate_binding", "other", "active").model_dump(mode="json")},
    ], expected_revision=manager.revision, request_id="duplicate-binding")
    assert result.status == "rejected"
    assert manager.get_snapshot() == before


def test_deleted_edge_is_not_restored_from_matching_inputs(state):
    manager = GraphManager(load_root_schema(), state)
    result = manager.apply([
        {"operation": "delete_edge", "edge_id": "active_binding"},
    ], expected_revision=manager.revision, request_id="remove-edge")
    assert result.status == "applied", result.errors
    assert manager.get_node("concept").properties["targets"] == [["active"]]
    assert not manager.get_edges("concept")


def test_custom_relation_requires_specific_description(state):
    from cortex.schema.models import EdgeType

    manager = GraphManager(load_root_schema(), state)
    custom = edge("custom", "concept", "amount", "has_threshold")
    operations = [{"operation": "create_edge", "edge": custom.model_dump(mode="json")}]
    missing = manager.apply(operations, expected_revision=manager.revision, request_id="missing-contract")
    assert missing.status == "rejected" and missing.errors[0].code == "UNREGISTERED_RELATION"
    result = manager.apply(operations, expected_revision=manager.revision, request_id="custom",
        relation_contracts=(EdgeType(id="has_threshold", name="Has threshold",
            description="A business concept associates with a column supplying a threshold value.",
            from_node_types=["concept"], to_node_types=["column"],
            direction_semantics="Business meaning toward its threshold input.",
            evidence_requirement="A supported threshold role, not a generic numerical association."),))
    assert result.status == "applied", result.errors
    assert next(e for e in manager.get_snapshot().graph.edges if e.id == "custom").edge_type == "has_threshold"
    invalid = custom.model_copy(update={"id": "bad", "properties": {}})
    result = manager.apply([{"operation": "create_edge", "edge": invalid.model_dump(mode="json")}], expected_revision=manager.revision, request_id="blank-description")
    assert result.status == "rejected"


def test_ordinary_projection_reads_stored_edges_without_recompilation(state, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A read must not infer relations from matching inputs")
    monkeypatch.setattr(GraphManager, "prepare_state", forbidden)
    graph, _ = project_graph(schema=load_root_schema(), state=state, selected_node_ids={"concept"}, choice_points=(), selected_by_point={})
    assert any(e.id == "active_binding" for e in graph.edges)
    assert not any(e.from_node_id == "concept" and e.to_node_id == "amount" for e in graph.edges)


def test_online_payload_and_agent_schema_use_nested_targets(state):
    from cortex.capabilities.online_context.output.projection import (
        semantic_asset_payload,
    )
    from cortex.capabilities.sql_training.learning.inputs import ontology_contract
    owner = next(n for n in state.graph.nodes if n.id == "concept")
    assert semantic_asset_payload(owner)["targets"] == [["active"]]
    schema = ontology_contract(load_root_schema())["output_schema"]["properties_by_type"]["concept"]
    assert schema["properties"]["targets"]["items"]["type"] == "array"


def test_read_services_reject_legacy_targets_before_calling_agents(state):
    import asyncio

    from cortex.application.online_context import OnlineContextService
    owner = next(n for n in state.graph.nodes if n.id == "concept").model_copy(deep=True)
    owner.properties["targets"] = [{"id": "old", "sql_expression": "customers.status", "references": ["status"]}]
    invalid = state.model_copy(update={"graph": GraphSpace(nodes=tuple(owner if n.id == owner.id else n for n in state.graph.nodes), edges=state.graph.edges)})
    with pytest.raises(GraphRejected):
        asyncio.run(OnlineContextService(object(), schema=load_root_schema()).run(question="customers", graph_state=invalid))


def choice(refs):
    """Shared explicit-choice fixture used by graph conflict consumer tests."""
    from cortex.application.online_context import ChoicePoint, InteractionOption

    return ChoicePoint(
        choice_point_id="choose",
        decision_question="Which implementation?",
        options=tuple(
            InteractionOption(
                option_id=f"o{i}", statement=ref, graph_refs=(ref,),
                candidate_sources=("memory_graph",),
            )
            for i, ref in enumerate(refs)
        ),
    )
