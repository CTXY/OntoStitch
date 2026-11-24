"""Group knowledge remains intact across real authoring and projection boundaries."""

import pytest
from test_explicit_relations import concept, edge, edit_tool, write

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import load_root_schema
from cortex.schema.models import EdgeType
from cortex.schema.relation_contracts import authoring_schema, effective_schema


def test_attribute_contract_is_one_level_nonmutating_and_not_widened(case_graph):
    store = case_graph[0]
    contract = EdgeType(id="has_profile", name="Profile", extends="has_attribute",
                        description="Associates an object with its profile attribute.",
                        from_node_types=["concept"], to_node_types=["concept"],
                        direction_semantics="Object to profile.", evidence_requirement="Supported business profile role.")
    result = write(edit_tool(case_graph), store, "profile", upserts=[concept("base"), concept("profile")],
                   relation_contracts=[contract.model_dump(mode="json")],
                   edge_operations=[{"operation": "create_edge", "edge": edge("profile", kind="has_profile", source="concept.base", target="concept.profile")}])
    assert result["status"] == "ok", result
    graph = store.working.graph
    raw = graph.model_dump(mode="json")
    schema = effective_schema(store.schema, graph)
    effective_schema(schema, graph)
    assert graph.model_dump(mode="json") == raw
    bad = contract.model_copy(update={"id": "bad", "to_node_types": ["column"]})
    with pytest.raises(ValueError, match="widen"):
        authoring_schema(store.schema, graph.model_copy(update={"relation_contracts": (bad,)}))
    with pytest.raises(ValueError):
        EdgeType.model_validate({**contract.model_dump(), "extends": "has_profile"})


def test_empty_metadata_keeps_legacy_envelope_shape():
    asset = NodeAsset.model_validate(concept("ordinary"))
    assert "relation_metadata" not in asset.model_dump(mode="json")["properties"]
    assert "extends" not in load_root_schema().edge_type_index()["has_attribute"].model_dump(mode="json")
