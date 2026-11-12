"""Core authoring is small; read compatibility does not grant write permission."""

import pytest
from test_explicit_relations import concept, edit_tool, write

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.sql_training.learning.inputs import ontology_contract
from cortex.schema import GraphEdge, GraphState, load_root_schema
from cortex.schema.models import EdgeType
from cortex.schema.relation_contracts import authoring_schema, effective_schema
from cortex.schema.relation_vocabulary import (
    CORE_RELATIONS,
    RETIRED_RELATIONS,
    legacy_relation_contracts,
)


def attribute_contract():
    return EdgeType(
        id="has_contact_detail", name="Contact detail",
        description="The target is a supported attribute role of the source business object type.",
        from_node_types=["concept"], to_node_types=["concept"],
        direction_semantics="Object type to attribute meaning; no universal existence or cardinality inferred.",
        evidence_requirement="The case and schema must support the attribute correspondence.",
    )


def proposal(kind="has_contact_detail"):
    return {"operation": "create_edge", "edge": {
        "id": "edge.attribute", "edge_type": kind,
        "from_node_id": "concept.object", "to_node_id": "concept.attribute",
        "properties": {"description": "The second concept describes an attribute of the first."},
    }}


def test_default_root_and_authoring_inventory_have_eight_relations():
    schema = load_root_schema()
    assert set(schema.edge_type_index()) == CORE_RELATIONS
    assert len(CORE_RELATIONS) == 8
    assert {r["type"] for r in ontology_contract(schema)["relations"]} == CORE_RELATIONS
    assert len(legacy_relation_contracts()) == 14


def test_optional_name_requires_local_contract_and_is_then_reusable(case_graph):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    nodes = [concept("object"), concept("attribute")]
    before = store.working_revision
    failed = write(tool, store, "undeclared_optional", upserts=nodes, edge_operations=[proposal()])
    assert failed["status"] == "rejected"
    assert failed["errors"][0]["code"] == "UNREGISTERED_RELATION"
    assert store.working_revision == before
    saved = write(tool, store, "declared_optional", upserts=nodes,
                  relation_contracts=[attribute_contract().model_dump(mode="json")],
                  edge_operations=[proposal()])
    assert saved["status"] == "ok", saved
    assert store.working.graph.relation_contracts == (attribute_contract(),)
    assert "has_contact_detail" in authoring_schema(store.schema, store.working.graph).edge_type_index()


@pytest.mark.parametrize("kind", sorted(RETIRED_RELATIONS))
def test_learning_cannot_reintroduce_retired_name_even_with_contract(case_graph, kind):
    store, _, _ = case_graph
    before = store.working_revision
    result = write(edit_tool(case_graph), store, "retired",
                   upserts=[concept("object"), concept("attribute")],
                   relation_contracts=[attribute_contract().model_copy(update={"id": kind}).model_dump(mode="json")],
                   edge_operations=[proposal(kind)])
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "RETIRED_RELATION"
    assert store.working_revision == before


def test_historical_relation_is_readable_but_does_not_authorize_new_edges(case_graph):
    store, _, _ = case_graph
    assert write(edit_tool(case_graph), store, "nodes", upserts=[concept("object"), concept("attribute")])["status"] == "ok"
    graph = store.working.graph
    old = GraphEdge.model_validate(proposal("related_to")["edge"])
    legacy = graph.model_copy(update={"edges": (*graph.edges, old)})
    manager = GraphManager(store.schema, GraphState(graph=legacy))
    assert manager.get_edges(edge_type="related_to") == [old]
    assert "related_to" in effective_schema(store.schema, legacy).edge_type_index()
    assert "related_to" not in authoring_schema(store.schema, legacy).edge_type_index()
    assert "related_to" not in {r["type"] for r in ontology_contract(store.schema)["relations"]}
    result = manager.apply([{"operation": "create_edge", "edge": old.model_copy(update={"id": "new_edge"}).model_dump(mode="json")}],
                           expected_revision=manager.revision, request_id="new_legacy")
    assert result.status == "rejected"
    assert result.errors[0].code == "UNREGISTERED_RELATION"
    assert manager.get_snapshot().graph == legacy


def test_has_attribute_is_abstract_even_though_its_contract_is_core(case_graph):
    store, _, _ = case_graph
    result = write(edit_tool(case_graph), store, "core_attribute",
                   upserts=[concept("object"), concept("attribute")],
                   edge_operations=[proposal("has_attribute")])
    assert result["status"] == "rejected", result
    assert "abstract relation base" in str(result)
    assert not store.working.graph.relation_contracts
    assert not any(e.edge_type == "has_attribute" for e in store.working.graph.edges)
