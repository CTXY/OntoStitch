"""Graph-local contracts travel with facts and never mutate the root schema."""

import json

import pytest
from test_agent_runtime import session_for

from cortex.capabilities.sql_training.graph.snapshots import GraphStore, graph_diff
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.infrastructure.artifacts.graph_state import (
    FileSystemGraphStateRepository,
    GraphStateBundleError,
)
from cortex.schema import GraphSpace, GraphState, load_root_schema
from cortex.schema.models import EdgeType
from cortex.schema.relation_contracts import effective_schema, merge_relation_contracts


def contract(**updates):
    return EdgeType.model_validate({
        "id": "serves", "name": "serves", "description": "A provider serves a recipient.",
        "from_node_types": ["concept"], "to_node_types": ["concept"],
        "direction_semantics": "Provider to recipient.",
        "evidence_requirement": "Evidence identifies both roles in the same service relationship.",
        **updates,
    })


def proposal(kind="serves"):
    return {
        "upserts": [{"id": f"concept.{name}", "node_type": "concept", "name": name,
                     "properties": {"targets": [["column.orders.amount"]]}} for name in ("provider", "recipient")],
        "edge_operations": [{"operation": "create_edge", "edge": {
            "id": "edge.service", "edge_type": kind,
            "from_node_id": "concept.provider", "to_node_id": "concept.recipient",
            "properties": {"description": "The provider supplies the service to the recipient."},
        }}],
    }


def write(case_graph, name, **changes):
    store = case_graph[0]
    tool = next(t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph")
    return tool.invoke({"edit_id": name, "expected_revision": store.working_revision, **changes})


def test_empty_contracts_preserve_legacy_json_and_schema():
    assert GraphSpace().model_dump(mode="json") == {"nodes": [], "edges": []}
    base = load_root_schema()
    before = base.model_dump(mode="json")
    local = GraphSpace(relation_contracts=(contract(),))
    assert "serves" in effective_schema(base, local).edge_type_index()
    assert base.model_dump(mode="json") == before
    assert GraphState.model_validate(local.model_dump(mode="json")).graph == local
    with pytest.raises(ValueError, match="redefined"):
        merge_relation_contracts((contract(),), (contract(description="Different meaning"),))
    with pytest.raises(ValueError, match="override"):
        effective_schema(base, GraphSpace(relation_contracts=(contract(id="is_a"),)))


def test_unknown_new_edges_rejected_and_contract_failure_is_atomic(case_graph):
    store = case_graph[0]
    initial = store.working_revision
    result = write(case_graph, "unknown", **proposal())
    assert result["status"] == "rejected"
    assert store.working_revision == initial
    bad = contract(to_node_types=["event"])
    result = write(case_graph, "wrong_endpoint", **proposal(), relation_contracts=[bad.model_dump(mode="json")])
    assert result["status"] == "rejected"
    assert store.working_revision == initial
    assert not store.working.graph.relation_contracts


def test_contract_roundtrip_accept_retain_and_guide(case_graph, tmp_path):
    store = case_graph[0]
    accepted = store.accepted_revision
    result = write(case_graph, "service", **proposal(), relation_contracts=[contract().model_dump(mode="json")])
    assert result["status"] == "ok", result
    proposed = store.working
    assert proposed.graph.relation_contracts == (contract(),)
    reopened = GraphStore(store.root, workspace=store.workspace, schema=store.schema)
    reopened.restore(proposed.revision, accepted)
    assert reopened.working.graph.relation_contracts == (contract(),)
    inspection = next(t for t in build_tools(session_for(case_graph)) if t.name == "inspect_graph")
    guide = inspection.invoke({"mode": "relations", "edge_type": "serves"})
    assert guide["relations"][0]["direction_semantics"] == contract().direction_semantics
    assert guide["unregistered_relation_types"] == []
    partial = store.select(assessed_revision=proposed.revision, accepted_before=accepted,
                           member_ids=["concept.provider"])
    assert not partial.graph.relation_contracts  # No unaccepted contract leaks.
    selected = store.select(assessed_revision=proposed.revision, accepted_before=accepted,
                            member_ids=graph_diff(store.accepted, proposed))
    assert selected.graph.relation_contracts == (contract(),)
    store.retain()
    assert not store.working.graph.relation_contracts
    repository = FileSystemGraphStateRepository(tmp_path / "formal", store.schema)
    repository.save(GraphState(graph=selected.graph))
    assert repository.load().graph.relation_contracts == (contract(),)
    generation = repository.root / "generations" / repository.revision
    manifest = json.loads((generation / "manifest.json").read_text())
    assert manifest["relation_contracts"]["count"] == 1
    (generation / "relation_contracts.jsonl").write_text("{}\n")
    with pytest.raises(GraphStateBundleError, match="hash mismatch"):
        repository.load()


def test_contract_survives_incoming_merge_and_retrieval(case_graph):
    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.graph_merge.transactions import apply_merge_state
    from cortex.capabilities.retrieve_db.graph.context import (
        build_retrieved_graph_state,
    )
    from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph

    store = case_graph[0]
    baseline = GraphState(graph=store.accepted.graph)
    result = write(case_graph, "incoming", **proposal(), relation_contracts=[contract().model_dump(mode="json")])
    assert result["status"] == "ok", result
    candidate = GraphState(graph=store.working.graph)
    manager = GraphManager(store.schema, baseline)
    merged = apply_merge_state(manager, candidate, request_id="merge-contract")
    assert merged.graph.relation_contracts == (contract(),)
    retrieved = build_retrieved_graph_state(merged, {n.id for n in merged.graph.nodes})
    assert retrieved.graph.relation_contracts == (contract(),)
    incoming = build_incoming_graph(
        store.schema, assets=(), existing=baseline.graph,
        cases=(("case", store.working.semantic_assets, store.working.explicit_edges),),
        relation_contracts=store.working.graph.relation_contracts,
    )
    assert incoming.graph.relation_contracts == (contract(),)
    assert any(edge.edge_type == "serves" for edge in incoming.graph.edges)


def test_legacy_unknown_relation_is_readable_and_reported(case_graph):
    from cortex.capabilities.sql_training.models import NodeAsset
    from cortex.schema import GraphEdge

    store = case_graph[0]
    data = proposal()
    snapshot = store.save(
        [*store.working.assets, *(NodeAsset.model_validate(node) for node in data["upserts"])],
        parent_revision=store.working_revision,
        explicit_edges=[GraphEdge.model_validate(data["edge_operations"][0]["edge"])],
    )
    store.working_revision = snapshot.revision
    session = session_for(case_graph)
    inspection = next(t for t in build_tools(session) if t.name == "inspect_graph")
    guide = inspection.invoke({"mode": "relations", "edge_type": "serves"})
    assert guide["unregistered_relation_types"] == ["serves"]
    assert guide["custom_relation"] is True


def test_registering_contract_for_unchanged_legacy_edge_requires_assessment(case_graph):
    from cortex.capabilities.sql_training.models import NodeAsset
    from cortex.schema import GraphEdge

    store = case_graph[0]
    data = proposal()
    legacy = store.save(
        [*store.working.assets, *(NodeAsset.model_validate(node) for node in data["upserts"])],
        parent_revision=store.working_revision,
        explicit_edges=[GraphEdge.model_validate(data["edge_operations"][0]["edge"])],
    )
    store.restore(legacy.revision, legacy.revision)
    raw = legacy.explicit_edges[0].model_dump(mode="json")
    for field in ("sources", "reference_origin"):
        raw.pop(field, None)
    result = write(case_graph, "register", relation_contracts=[contract().model_dump(mode="json")],
                   edge_operations=[{"operation": "update_edge", "edge": raw}])
    assert result["status"] == "ok", result
    proposed = store.working
    # Ignore code-maintained provenance differences: the contract alone is a change.
    identical_edges = proposed.model_copy(update={"explicit_edges": legacy.explicit_edges})
    assert graph_diff(legacy, identical_edges) == ["edge.service"]
    selected = store.select(assessed_revision=proposed.revision,
                            accepted_before=legacy.revision, member_ids=["edge.service"])
    assert selected.graph.relation_contracts == (contract(),)


def test_new_contract_couples_disconnected_legacy_edges_for_acceptance(case_graph):
    from cortex.capabilities.sql_training.graph.mutations import (
        ChangeGroup,
        normalize_groups,
    )
    from cortex.capabilities.sql_training.models import NodeAsset
    from cortex.schema import GraphEdge

    store = case_graph[0]
    data = proposal()
    originals = [NodeAsset.model_validate(node) for node in data["upserts"]]
    second_pair = [node.model_copy(update={"id": node.id + "2", "name": node.name + "2"})
                   for node in originals]
    first = GraphEdge.model_validate(data["edge_operations"][0]["edge"])
    second = first.model_copy(update={"id": "edge.service2", "from_node_id": "concept.provider2",
                                      "to_node_id": "concept.recipient2"})
    legacy = store.save([*store.working.assets, *originals, *second_pair],
                        parent_revision=store.working_revision, explicit_edges=[first, second])
    store.restore(legacy.revision, legacy.revision)
    raw = first.model_dump(mode="json", exclude={"sources", "reference_origin"})
    result = write(case_graph, "register_two", relation_contracts=[contract().model_dump(mode="json")],
                   edge_operations=[{"operation": "update_edge", "edge": raw}])
    assert result["status"] == "ok", result
    proposed = store.working
    groups = normalize_groups([
        ChangeGroup(group_id="one", member_ids=[first.id]),
        ChangeGroup(group_id="two", member_ids=[second.id]),
    ], legacy, proposed)
    assert len(groups) == 1
    assert set(groups[0].member_ids) == {first.id, second.id}
    with pytest.raises(ValueError, match="every edge affected"):
        store.select(assessed_revision=proposed.revision, accepted_before=legacy.revision,
                     member_ids=[first.id])
    assert store.accepted_revision == legacy.revision
    assert not store.accepted.graph.relation_contracts
    selected = store.select(assessed_revision=proposed.revision, accepted_before=legacy.revision,
                            member_ids=[first.id, second.id])
    assert selected.graph.relation_contracts == (contract(),)


def test_selecting_unrelated_node_does_not_install_tentative_legacy_contract(case_graph):
    from cortex.capabilities.sql_training.models import NodeAsset
    from cortex.schema import GraphEdge

    store = case_graph[0]
    data = proposal()
    assets = [NodeAsset.model_validate(node) for node in data["upserts"]]
    unrelated = assets[0].model_copy(update={"id": "concept.unrelated", "name": "Unrelated"})
    edge = GraphEdge.model_validate(data["edge_operations"][0]["edge"])
    legacy = store.save([*store.working.assets, *assets, unrelated],
                        parent_revision=store.working_revision, explicit_edges=[edge])
    store.restore(legacy.revision, legacy.revision)
    changed_node = unrelated.model_copy(update={"name": "Updated unrelated meaning"})
    raw = edge.model_dump(mode="json", exclude={"sources", "reference_origin"})
    result = write(case_graph, "register_and_update", relation_contracts=[contract().model_dump(mode="json")],
                   upserts=[changed_node.model_dump(mode="json", exclude={"sources"})],
                   edge_operations=[{"operation": "update_edge", "edge": raw}])
    assert result["status"] == "ok", result
    selected = store.select(assessed_revision=store.working_revision, accepted_before=legacy.revision,
                            member_ids=[unrelated.id])
    assert next(node.name for node in selected.assets if node.id == unrelated.id) == changed_node.name
    assert selected.explicit_edges == legacy.explicit_edges
    assert selected.graph.relation_contracts == legacy.graph.relation_contracts == ()
