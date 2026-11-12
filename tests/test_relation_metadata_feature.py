"""Group knowledge remains intact across real authoring and projection boundaries."""
from copy import deepcopy

import pytest
from test_explicit_relations import concept, edge, edit_tool, write

from cortex.capabilities.retrieve_db.graph.context import build_retrieved_graph_state
from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.assessment_inputs import (
    application_requirement,
)
from cortex.schema import GraphState, load_root_schema
from cortex.schema.models import EdgeType
from cortex.schema.relation_contracts import authoring_schema, effective_schema


def populate(case_graph):
    store = case_graph[0]
    parent = concept("base")
    parent["properties"]["relation_metadata"] = [{
        "id": "members", "edges": ["edge.one", "edge.two"],
        "description": "The two classifications are not declared mutually exclusive.",
    }]
    result = write(edit_tool(case_graph), store, "metadata", upserts=[parent, concept("one"), concept("two")],
                   edge_operations=[{"operation": "create_edge", "edge": edge("one", source="concept.one", target="concept.base")},
                                    {"operation": "create_edge", "edge": edge("two", source="concept.two", target="concept.base")}])
    assert result["status"] == "ok", result
    return store


def test_atomic_group_write_and_dangling_delete_rejection(case_graph):
    store = populate(case_graph)
    revision = store.working_revision
    result = write(edit_tool(case_graph), store, "bad_delete", edge_operations=[{"operation": "delete_edge", "edge_id": "edge.one"}])
    assert result["status"] == "rejected"
    assert store.working_revision == revision
    owner = next(a for a in store.working.assets if a.id == "concept.base").model_dump(mode="json")
    owner["properties"].pop("relation_metadata")
    result = write(edit_tool(case_graph), store, "good_delete", upserts=[owner], edge_operations=[{"operation": "delete_edge", "edge_id": "edge.one"}])
    assert result["status"] == "ok", result


def test_projection_omits_whole_group_without_changing_original(case_graph):
    store = populate(case_graph)
    state = GraphState(graph=store.working.graph)
    source = state.model_dump(mode="json")
    partial = build_retrieved_graph_state(state, {"concept.base", "concept.one"})
    assert "relation_metadata" not in next(n for n in partial.graph.nodes if n.id == "concept.base").properties
    complete = build_retrieved_graph_state(state, {"concept.base", "concept.one", "concept.two"})
    assert next(n for n in complete.graph.nodes if n.id == "concept.base").properties["relation_metadata"][0]["edges"] == ["edge.one", "edge.two"]
    assert state.model_dump(mode="json") == source


def test_export_rewrites_edge_ids_and_retains_exact_group(case_graph):
    store = populate(case_graph)
    snapshot = store.working
    physical = snapshot.physical_assets
    semantic = snapshot.semantic_assets
    incoming = build_incoming_graph(store.schema, assets=physical, cases=[("example", semantic, snapshot.explicit_edges)],
                                    catalog_edges=snapshot.catalog_edges)
    owner = next(n for n in incoming.graph.nodes if n.name == "base")
    group = owner.properties["relation_metadata"][0]
    assert len(group["edges"]) == 2
    assert all(ref.startswith("incoming_edge_") for ref in group["edges"])
    assert set(group["edges"]) <= {e.id for e in incoming.graph.edges}


def test_group_shape_and_anchor_are_checked(case_graph):
    store = populate(case_graph)
    owner = next(a for a in store.working.assets if a.id == "concept.base").model_dump(mode="json")
    for members in (["edge.one", "edge.one"], ["edge.one", "missing"], ["edge.one"]):
        changed = deepcopy(owner)
        changed["properties"]["relation_metadata"][0]["edges"] = members
        result = write(edit_tool(case_graph), store, "bad_"+str(len(members))+str(members[-1]).replace(".","_"), upserts=[changed])
        assert result["status"] == "rejected"


def test_metadata_only_uses_accepted_diff_without_an_agent_flag():
    before = {"id": "concept.a", "node_type": "concept", "properties": {"targets": [["column.t.id"]]}}
    after = deepcopy(before)
    after["properties"]["relation_metadata"] = [{"id": "g", "edges": ["a", "b"], "description": "Group meaning"}]
    assert application_requirement(after, accepted_before=before) == "semantic"
    assert application_requirement(after) == "sql"
    mixed = deepcopy(after)
    mixed["properties"]["targets"] = [["column.t.other"]]
    assert application_requirement(mixed, accepted_before=before) == "sql"
    assert "relation_metadata" not in before["properties"]


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


def test_assessment_sees_metadata_on_unchanged_owner_after_member_edit(case_graph):
    from conftest import seed_accepted
    from test_agent_feedback import payload_for
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.graph.mutations import normalize_groups

    store = populate(case_graph)
    seed_accepted(store)
    before = store.accepted
    altered = edge("one", source="concept.one", target="concept.base", description="A more precise supported category inclusion.")
    result = write(edit_tool(case_graph), store, "edge_only", edge_operations=[{"operation": "update_edge", "edge": altered}])
    assert result["status"] == "ok", result
    payload = payload_for(session_for(case_graph), before, normalize_groups([], before, store.working))
    owner = next(n for n in payload["related_knowledge"]["nodes"] if n["id"] == "concept.base")
    assert owner["properties"]["relation_metadata"][0]["edges"] == ["edge.one", "edge.two"]
    assert any(e["id"] == "edge.two" for e in payload["related_knowledge"]["edges"])
    assert not any(e["id"] == "edge.one" for e in payload["related_knowledge"]["edges"])
    assert "relation_metadata_consistency" not in payload["quality_rubric"]


def test_real_metadata_only_unit_uses_semantic_application(case_graph):
    from conftest import seed_accepted
    from test_agent_feedback import payload_for
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.graph.mutations import normalize_groups

    store = populate(case_graph)
    seed_accepted(store)
    before = store.accepted
    owner = next(a for a in before.assets if a.id == "concept.base").model_dump(mode="json")
    owner["properties"]["relation_metadata"][0]["description"] = "The declared categories may share members within the case scope."
    result = write(edit_tool(case_graph), store, "description_only", upserts=[owner])
    assert result["status"] == "ok", result
    payload = payload_for(session_for(case_graph), before, normalize_groups([], before, store.working))
    assert len(payload["units"]) == 1
    assert payload["units"][0]["application_requirement"] == "semantic"


@pytest.mark.parametrize("change", [
    {"edge_type": "has_attribute"},
    {"from_node_id": "concept.base", "to_node_id": "concept.two"},
    {"from_node_id": "concept.one", "to_node_id": "concept.two"},
])
def test_group_rejects_mixed_predicate_direction_or_nonadjacent_edge(case_graph, change):
    store = populate(case_graph)
    revision = store.working_revision
    altered = {**edge("two", source="concept.two", target="concept.base"), **change}
    result = write(edit_tool(case_graph), store, "bad_shape", edge_operations=[{"operation": "update_edge", "edge": altered}])
    assert result["status"] == "rejected"
    assert store.working_revision == revision


def test_partial_selection_cannot_leave_group_pointing_at_unaccepted_edge(case_graph):
    from conftest import seed_accepted
    store = populate(case_graph)
    seed_accepted(store)
    before = store.accepted_revision
    owner = next(a for a in store.working.assets if a.id == "concept.base").model_dump(mode="json")
    owner["properties"]["relation_metadata"][0]["edges"] = ["edge.one", "edge.three"]
    result = write(edit_tool(case_graph), store, "third", upserts=[owner, concept("three")],
                   edge_operations=[{"operation": "create_edge", "edge": edge("three", source="concept.three", target="concept.base")}])
    assert result["status"] == "ok", result
    with pytest.raises(ValueError, match="missing edge"):
        store.select(assessed_revision=store.working_revision, accepted_before=before, member_ids=["concept.base"])
    assert store.accepted_revision == before


@pytest.mark.parametrize("absent", [False, True])
def test_conflict_group_remap_and_partial_projection_are_safe(case_graph, absent):
    from cortex.schema.graph import ConflictAlternative, NodeMergeConflict
    from cortex.schema.relation_metadata import (
        remap_metadata_edges,
        validate_relation_metadata,
    )

    store = populate(case_graph)
    state = GraphState(graph=store.working.graph)
    owner = next(n for n in state.graph.nodes if n.id == "concept.base")
    groups = owner.properties["relation_metadata"]
    second = deepcopy(groups)
    second[0]["description"] = "The categories are mutually exclusive within the stated scope."
    if absent:
        second = None
    overrides = {"properties.relation_metadata": groups}
    mapped = remap_metadata_edges(overrides, {"edge.one": "edge.replaced"})
    assert mapped["properties.relation_metadata"][0]["edges"] == ["edge.replaced", "edge.two"]
    assert remap_metadata_edges({"properties.relation_metadata": None}, {}) == {"properties.relation_metadata": None}
    assert overrides["properties.relation_metadata"][0]["edges"] == ["edge.one", "edge.two"]
    props = {k: v for k, v in owner.properties.items() if k != "relation_metadata"}
    conflicted = owner.model_copy(update={"properties": props}).with_conflict(NodeMergeConflict(
        node_id=owner.id, alternatives=(
            ConflictAlternative(alternative_id="a", overrides=overrides, sources=owner.sources),
            ConflictAlternative(alternative_id="b", overrides={"properties.relation_metadata": second}, sources=owner.sources),
        ),
    ))
    state = GraphState(graph=state.graph.model_copy(update={"nodes": tuple(conflicted if n.id == owner.id else n for n in state.graph.nodes)}))
    complete = build_retrieved_graph_state(state, {n.id for n in state.graph.nodes})
    validate_relation_metadata(complete.graph)
    assert next(n for n in complete.graph.nodes if n.id == owner.id).as_conflict().alternatives == conflicted.alternatives
    with pytest.raises(ValueError, match="Cannot project conflict"):
        build_retrieved_graph_state(state, {"concept.base", "concept.one"})


def test_base_assets_remap_group_catalog_edge_ids(case_graph):
    store = populate(case_graph)
    snapshot = store.working
    incoming = build_incoming_graph(
        store.schema, assets=snapshot.assets, cases=(),
        catalog_edges=iter(snapshot.graph.edges),
    )
    owner = next(n for n in incoming.graph.nodes if n.id == "concept.base")
    refs = owner.properties["relation_metadata"][0]["edges"]
    assert all(ref.startswith("incoming_edge_") for ref in refs)
    assert set(refs) <= {e.id for e in incoming.graph.edges}
