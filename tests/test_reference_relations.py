"""Agents author saved relations; references are never a second graph."""

import pytest
from relation_fixtures import metric_context_contract
from test_graph_manager import apply, create, metric, node

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.online_context.output.projection import project_graph
from cortex.schema import GraphSpace, load_root_schema


@pytest.fixture
def manager():
    manager = GraphManager(load_root_schema())
    result = apply(manager,
        create(node("t", "table", {"table_name": "orders"})),
        create(node("amount", "column", {"table_ref": "t", "column_name": "amount"})),
        create(node("region", "column", {"table_ref": "t", "column_name": "region"})),
        relation_contracts=(metric_context_contract(),),
    )
    assert result.status == "applied", result.errors
    return manager


def explicit(kind="custom_measures", source="m", target="region", name="custom", **props):
    return {"operation": "create_edge", "edge": {
        "id": name, "edge_type": kind, "from_node_id": source, "to_node_id": target,
        "properties": {"description": "An independently evidenced business relationship.", **props},
    }}


def test_business_edge_coexists_with_formula_edge_instead_of_overriding_it(manager):
    assert apply(manager, create(metric("m", "amount")), explicit()).status == "applied"
    assert {(e.edge_type, e.to_node_id) for e in manager.get_edges("m")} == {
        ("grounded_on", "amount"), ("custom_measures", "region"),
    }
    assert all(e.reference_origin is None for e in manager.get_edges("m"))


def test_missing_formula_relation_rejects_without_default_edge_restoration(manager):
    assert apply(manager, create(metric("m", "amount"))).status == "applied"
    before = manager.get_snapshot()
    relation = next(e for e in manager.get_edges("m") if e.edge_type == "grounded_on")
    result = manager.apply([{"operation": "delete_edge", "edge_id": relation.id}], expected_revision=manager.revision, request_id="remove-operand")
    assert result.status == "rejected"
    assert result.errors[0].code == "MISSING_FORMULA_RELATION"
    assert manager.get_snapshot() == before


def test_formula_edit_and_explicit_edge_replacement_are_atomic(manager):
    assert apply(manager, create(metric("m", "amount")), explicit()).status == "applied"
    updated = metric("m", "region")
    before = manager.get_snapshot()
    rejected = manager.apply([{"operation": "update_node", "node": updated}], expected_revision=manager.revision, request_id="incomplete")
    assert rejected.status == "rejected"
    assert manager.get_snapshot() == before
    assert apply(manager, {"operation": "update_node", "node": updated}).status == "applied"
    assert {e.to_node_id for e in manager.get_edges("m", "grounded_on")} == {"region"}
    assert manager.get_edges("m", "custom_measures")


def test_multiple_match_groups_do_not_require_a_relation_per_group(manager):
    meaning = node("meaning", "concept", {"targets": [["amount"], ["region"]]})
    assert apply(manager, create(meaning)).status == "applied"
    assert manager.get_edges("meaning") == []
    assert apply(manager, explicit("grounded_on", "meaning", "amount")).status == "applied"
    assert {e.to_node_id for e in manager.get_edges("meaning")} == {"amount"}


def test_custom_relation_obeys_described_endpoint_facts_and_fixed_names_obey_types(manager):
    assert apply(manager, create(metric("m", "amount")), explicit(settings={"x": [1, True]})).status == "applied"
    saved = manager.get_edges("m", "custom_measures")[0]
    assert saved.properties["settings"] == {"x": [1, True]}
    before = manager.get_snapshot()
    rejected = apply(manager, explicit(kind="joins_with", name="bad"))
    assert rejected.status == "rejected"
    assert rejected.errors[0].code == "INVALID_ENDPOINT_TYPE"
    assert manager.get_snapshot() == before


def test_relationship_reader_preserves_custom_edges_without_recompilation(manager, monkeypatch):
    assert apply(manager, create(metric("m", "amount")), explicit()).status == "applied"
    state = manager.get_snapshot()
    monkeypatch.setattr(GraphManager, "prepare_state", lambda *a, **kw: pytest.fail("read inferred relations"))
    graph, _ = project_graph(schema=manager.schema, state=state, selected_node_ids={"m"}, choice_points=(), selected_by_point={})
    assert any(e.id == "custom" for e in graph.edges)
    assert any(e.edge_type == "grounded_on" for e in graph.edges)


def test_description_is_required_and_cannot_be_replaced_by_role_or_qualifier(manager):
    assert apply(manager, create(metric("m", "amount"))).status == "applied"
    for props in ({"description": ""}, {"role": "payer"}, {"qualifier": "sometimes"}):
        result = manager.apply([explicit(**props)], expected_revision=manager.revision, request_id=str(props))
        assert result.status == "rejected"


def test_nonidentical_scope_descriptions_are_not_silently_overwritten(manager):
    assert apply(manager, create(metric("m", "amount")), explicit(description="Applies to the historical source.")).status == "applied"
    result = apply(manager, explicit(name="other", description="Applies to the current source."))
    assert result.status == "applied", result.errors
    assert len(manager.get_edges("m", "custom_measures")) == 2


def test_formal_state_does_not_infer_a_formula_edge_from_refs(manager):
    from cortex.schema import GraphNode, GraphState
    state = manager.get_snapshot()
    candidate = state.model_copy(update={"graph": GraphSpace(nodes=(*state.graph.nodes, GraphNode.model_validate(metric("m", "amount"))), edges=state.graph.edges)})
    with pytest.raises(GraphRejected, match="explicit grounded_on"):
        manager.prepare_state(GraphState(graph=candidate.graph))


def test_physical_owner_edges_are_the_only_deterministically_created_structure(manager):
    assert {e.edge_type for e in manager.get_edges()} == {"belongs_to"}
    assert all(e.properties["description"] for e in manager.get_edges())
    assert all(e.reference_origin == "default" for e in manager.get_edges())
