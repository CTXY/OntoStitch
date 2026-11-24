"""Reviewed previews preserve explicit facts across conflict transitions."""


import pytest
from ontology_v2_fixtures import formula_edge
from relation_fixtures import metric_context_contract
from test_conflict_writes import set_operation
from test_graph_manager import apply, create, node

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.publication import apply_merge_state
from cortex.schema import GraphSpace, load_root_schema


@pytest.fixture
def manager():
    manager = GraphManager(load_root_schema())
    result = apply(
        manager,
        create(node("table", "table", {"table_name": "orders"})),
        *[
            create(node(key, "column", {"table_ref": "table", "column_name": key}))
            for key in ("a", "b", "c")
        ],
        set_operation(manager),
        *[
            {
                "operation": "create_edge",
                "edge": formula_edge("m", ref, key).model_dump(mode="json"),
            }
            for key, ref in (("first", "a"), ("second", "b"))
        ],
        {
            "operation": "create_edge",
            "edge": {
                "id": "explicit",
                "edge_type": "custom_measures",
                "from_node_id": "m",
                "to_node_id": "a",
                "properties": {
                    "alternative_ids": ["first"],
                    "description": "This explicit branch fact relates the metric to its selected amount.",
                },
            },
        },
        relation_contracts=(metric_context_contract(),),
    )
    assert result.status == "applied", result.errors
    return manager


def test_relation_only_update_of_conflict_owner_uses_atomic_reconciliation(manager):
    before = manager.get_snapshot()
    updated = before.model_copy(
        update={
            "graph": GraphSpace(
                nodes=before.graph.nodes,
                edges=tuple(
                    edge.model_copy(
                        update={
                            "properties": {
                                **edge.properties,
                                "description": "Reviewed branch-specific relationship",
                            }
                        }
                    )
                    if edge.id == "explicit"
                    else edge
                    for edge in before.graph.edges
                ),
            )
        }
    )
    result = apply_merge_state(manager, updated, request_id="edge-only")
    assert result.conflict_nodes == before.conflict_nodes
    assert result.graph.nodes == before.graph.nodes
    explicit = next(edge for edge in result.graph.edges if edge.id == "explicit")
    assert explicit.properties == {
        "alternative_ids": ["first"],
        "description": "Reviewed branch-specific relationship",
    }
    manager.validate_state(result)
