"""Canonical inline nodes round-trip with legacy input imported only at boundaries."""

import copy
import json

import pytest
from pydantic import ValidationError

from cortex.schema import (
    ConflictAlternative,
    GraphNode,
    GraphSpace,
    GraphState,
    MergeStatus,
    NodeMergeConflict,
    SourceReference,
    export_inline_graph,
    with_conflicts,
)


@pytest.fixture
def state():
    sources = tuple(SourceReference(source_id=key) for key in ("case_a", "case_b"))
    return GraphState(
        graph=GraphSpace(
            nodes=(
                GraphNode(
                    id="metric.revenue",
                    node_type="metric",
                    name="Revenue",
                    properties={"pattern": "Revenue calculation"},
                    sources=sources,
                    merge_status="conflict",
                ),
                GraphNode(id="table.orders", node_type="table", name="Orders"),
            )
        ),
        node_conflicts=(
            NodeMergeConflict(
                node_id="metric.revenue",
                consolidation_status="semantic_conflict",
                preservation_reasons=("Distinct calculation definitions",),
                alternatives=tuple(
                    ConflictAlternative(
                        alternative_id=key,
                        overrides={"properties.expression": {"expr": expr, "refs": {}}},
                        sources=(source,),
                    )
                    for key, expr, source in zip(
                        ("gross", "net"), ("100", "90"), sources, strict=True
                    )
                ),
            ),
        ),
    )


def test_public_inline_roundtrip_and_no_duplicate_truth(state):
    before = state.model_dump(mode="json")
    payload = export_inline_graph(state)
    assert set(payload) == {"nodes", "edges"}
    node = payload["nodes"][0]
    assert "node_conflicts" not in payload and "node_id" not in node
    assert node["consolidation_status"] == "semantic_conflict"
    assert node["preservation_reasons"] and node["conflict_points"] == []
    assert all(a["sources"] and "content" not in a for a in node["alternatives"])
    assert "alternatives" not in payload["nodes"][1]
    assert GraphState.model_validate(payload) == state
    assert GraphState.model_validate({"graph": payload}) == state
    assert GraphState.model_validate_json(json.dumps(payload)) == state
    assert state.model_dump(mode="json") == before
    payload["nodes"][0]["alternatives"][0]["overrides"]["properties.expression"][
        "expr"
    ] = "changed"
    assert (
        state.node_conflicts[0]
        .alternatives[0]
        .overrides["properties.expression"]["expr"]
        == "100"
    )


def test_sidecar_compatibility_and_matching_mixed_input(state):
    legacy = {
        "graph": export_inline_graph(state),
        "node_conflicts": [
            node.as_conflict().model_dump(mode="json") for node in state.conflict_nodes
        ],
    }
    for node in legacy["graph"]["nodes"]:
        for key in (
            "consolidation_status",
            "preservation_reasons",
            "alternatives",
            "conflict_points",
        ):
            node.pop(key, None)
    assert GraphState.model_validate(legacy) == state
    mixed = {
        "graph": export_inline_graph(state),
        "node_conflicts": legacy["node_conflicts"],
    }
    assert GraphState.model_validate(mixed) == state
    mixed["node_conflicts"][0]["preservation_reasons"] = ["different reason"]
    with pytest.raises(ValidationError, match="disagree"):
        GraphState.model_validate(mixed)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_metadata",
        "normal_status",
        "missing_alternative_sources",
        "missing_node_sources",
        "wrong_union",
        "common_override",
    ],
)
def test_inconsistent_inline_rejected_without_mutating_input(state, mutation):
    payload = export_inline_graph(state)
    node = payload["nodes"][0]
    if mutation == "missing_metadata":
        node.pop("conflict_points")
    elif mutation == "normal_status":
        node["merge_status"] = "normal"
    elif mutation == "missing_alternative_sources":
        node["alternatives"][0]["sources"] = []
    elif mutation == "missing_node_sources":
        node["sources"] = []
    elif mutation == "wrong_union":
        node["sources"] = node["sources"][:1]
    else:
        node["properties"]["expression"] = {"expr": "100", "refs": {}}
    before = copy.deepcopy(payload)
    with pytest.raises(ValidationError):
        GraphState.model_validate(payload)
    assert payload == before


def test_export_rejects_missing_candidate_provenance_but_legacy_still_loads(state):
    payload = state.model_dump(mode="json")
    payload["graph"]["nodes"][0]["alternatives"][0]["sources"] = []
    payload["graph"]["nodes"][0]["sources"] = payload["graph"]["nodes"][0]["sources"][
        1:
    ]
    legacy = GraphState.model_validate(payload)
    with pytest.raises(ValueError, match="nonempty sources"):
        export_inline_graph(legacy)


def test_conflict_markers_require_inline_metadata(state):
    payload = export_inline_graph(state)
    for key in (
        "consolidation_status",
        "preservation_reasons",
        "alternatives",
        "conflict_points",
    ):
        payload["nodes"][0].pop(key)
    with pytest.raises(ValidationError, match="consolidation_status"):
        GraphState.model_validate(payload)


def test_empty_and_normal_inline_graphs():
    assert GraphState.model_validate({"nodes": [], "edges": []}) == GraphState(
        graph=GraphSpace(), node_conflicts=()
    )
    assert (
        GraphState.model_validate({"graph": {"nodes": [], "edges": []}}).node_conflicts
        == ()
    )


def test_state_has_only_one_stored_inline_truth(state):
    assert set(GraphState.model_fields) == {"graph"}
    assert "node_conflicts" not in GraphState.model_json_schema()["properties"]
    assert "node_conflicts" not in state.model_dump()
    assert state.conflict_nodes[0] is state.graph.nodes[0]
    assert state.conflict_nodes[0].alternatives
    normal = state.graph.nodes[1].model_dump(mode="json")
    assert not {
        "consolidation_status",
        "preservation_reasons",
        "alternatives",
        "conflict_points",
    }.intersection(normal)
    with pytest.raises(ValueError, match="import-only"):
        state.model_copy(update={"node_conflicts": ()})


def test_explicit_proposal_attachment_and_clearing(state):
    before = state.model_dump(mode="json")
    node = state.conflict_nodes[0]
    proposal = node.as_conflict()
    assert node.without_conflict().with_conflict(proposal) == node
    assert with_conflicts(state.graph, (proposal,)) == state
    assert with_conflicts(state.graph, (node,)) == state
    cleared = with_conflicts(state.graph, ())
    assert not cleared.conflict_nodes
    assert not cleared.graph.nodes[0].alternatives
    assert state.model_dump(mode="json") == before


def test_normal_node_rejects_metadata_in_construction_copy_and_serialization(state):
    node = state.conflict_nodes[0]
    with pytest.raises(ValidationError, match="normal nodes"):
        GraphNode(
            id="normal",
            node_type="metric",
            name="Normal",
            alternatives=node.alternatives,
        )
    with pytest.raises(ValueError, match="normal nodes"):
        node.model_copy(update={"merge_status": "normal"})
    with pytest.raises(ValueError, match="normal nodes"):
        state.graph.nodes[1].model_copy(
            update={"preservation_reasons": ("hidden metadata",)}
        )
    unchecked = GraphNode.model_construct(
        id="invalid", node_type="metric", name="Invalid", alternatives=node.alternatives
    )
    with pytest.raises(ValueError, match="normal nodes"):
        unchecked.model_dump(mode="json")


def test_invalid_merge_status_cannot_silently_drop_inline_metadata(state):
    node = state.conflict_nodes[0]
    before = node.model_dump(mode="json")
    with pytest.raises(ValueError, match="not a valid MergeStatus"):
        node.model_copy(update={"merge_status": "unknown"})
    unchecked = GraphNode.model_construct(**{**before, "merge_status": "unknown"})
    with pytest.raises(ValueError, match="not a valid MergeStatus"):
        unchecked.model_dump(mode="json")
    assert node.model_dump(mode="json") == before
    assert (
        node.model_copy(update={"merge_status": "conflict"}).merge_status
        is MergeStatus.CONFLICT
    )
    assert (
        node.without_conflict()
        .model_copy(update={"merge_status": "normal"})
        .merge_status
        is MergeStatus.NORMAL
    )
