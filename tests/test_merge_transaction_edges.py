"""Reviewed previews preserve explicit facts across conflict transitions."""

from types import SimpleNamespace

import pytest
from ontology_v2_fixtures import formula_edge
from relation_fixtures import metric_context_contract
from test_conflict_writes import set_operation
from test_graph_manager import apply, create, metric, node

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.identity import node_content
from cortex.capabilities.graph_merge.models import (
    FinalNodeGroup,
    NodeRepresentation,
    RepresentationRef,
)
from cortex.capabilities.graph_merge.transactions import (
    apply_merge_state,
    preview_group,
)
from cortex.capabilities.graph_merge.workflow.context import GroupContext
from cortex.schema import GraphNode, GraphSpace, load_root_schema


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


def context_for(manager):
    member = NodeRepresentation(
        ref=RepresentationRef(origin="existing_node", ref_id="m"),
        node_type="metric",
        content=node_content(manager.get_node("m")),
        sources=(),
    )
    return GroupContext(
        group=FinalNodeGroup(
            group_id="merge-m",
            node_type="metric",
            existing_node_id="m",
            members=(member,),
        ),
        schema_definition=manager.schema,
        existing_state=manager.get_snapshot(),
    )


def test_conflict_preview_and_actual_commit_preserve_explicit_branch_fact(manager):
    before, revision = manager.get_snapshot(), manager.revision
    replacement = GraphNode.model_validate(metric("m", "a"))
    preview = preview_group(
        context_for(manager), SimpleNamespace(node=replacement, conflict=None)
    )
    assert manager.get_snapshot() == before and manager.revision == revision
    assert not preview.conflict_nodes
    explicit = next(edge for edge in preview.graph.edges if edge.id == "explicit")
    assert explicit.edge_type == "custom_measures"
    assert explicit.reference_origin is None
    assert "alternative_ids" not in explicit.properties
    assert {
        edge.to_node_id for edge in preview.graph.edges if edge.from_node_id == "m"
    } == {"a"}
    actual = apply_merge_state(manager, preview, request_id="commit-preview")
    assert actual == preview
    manager.validate_state(actual)


def test_preview_requires_explicit_relation_for_new_formula_operand(manager):
    before, revision = manager.get_snapshot(), manager.revision
    with pytest.raises(GraphRejected) as caught:
        preview_group(
            context_for(manager),
            SimpleNamespace(
                node=GraphNode.model_validate(metric("m", "c")), conflict=None
            ),
        )
    assert any(
        issue.code == "MISSING_FORMULA_RELATION" for issue in caught.value.issues
    )
    assert manager.get_snapshot() == before and manager.revision == revision


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
