"""Explicit relation lifecycle through Agent, acceptance, persistence and consumers."""

import pytest
from conftest import seed_accepted
from test_agent_runtime import session_for

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.sql_training.graph.mutations import normalize_groups
from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph
from cortex.capabilities.sql_training.graph.snapshots import GraphStore, graph_diff
from cortex.capabilities.sql_training.learning.inputs import ontology_contract
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.schema import GraphSpace, GraphState


def concept(name):
    return {
        "id": f"concept.{name}",
        "node_type": "concept",
        "name": name,
        "properties": {
            "targets": [["column.orders.amount"]]
        },
    }


def edge(
    name="hierarchy",
    kind="is_a",
    source="concept.a",
    target="concept.b",
    **properties,
):
    return {
        "id": f"edge.{name}",
        "edge_type": kind,
        "from_node_id": source,
        "to_node_id": target,
        "properties": {"description": "An evidenced business relationship between the endpoints.", **properties},
    }


def edit_tool(case_graph):
    return next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )


def write(tool, store, name, **changes):
    # Test authoring supplies explicit formula edges for every conflict candidate.
    # Production must not recreate these from old generic refs.
    from test_graph_manager import authored_operations
    try:
        operations = authored_operations(store.manager(), changes.get("conflict_operations", []))
    except ValueError:
        operations = []  # Invalid fixture input must reach the real tool validator.
    authored = list(changes.get("edge_operations", []))
    known = {op.get("edge", {}).get("id") or op.get("edge_id") for op in authored}
    for op in operations:
        if op.get("operation") not in {"create_edge", "update_edge", "delete_edge"}:
            continue
        key = op.get("edge", {}).get("id") or op.get("edge_id")
        if key in known:
            continue
        if isinstance(op.get("edge"), dict):
            op["edge"].pop("sources", None)
            op["edge"].pop("reference_origin", None)
            op["edge"].get("properties", {}).pop("alternative_ids", None)
        authored.append(op)
    if authored:
        changes["edge_operations"] = authored
    return tool.invoke({"edit_id": name, "expected_revision": store.working_revision, **changes})


def seed_pair(case_graph):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    result = write(tool, store, "pair", upserts=[concept("a"), concept("b")])
    assert result["status"] == "ok", result
    return store, tool


def test_joint_nodes_and_different_relations_are_atomic_and_do_not_change_refs(
    case_graph,
):
    store, _, _ = case_graph
    old = store.accepted
    tool = edit_tool(case_graph)
    from cortex.schema.relation_vocabulary import legacy_relation_contracts

    edges = [
        edge(),
        edge("related", "has_feature"),
    ]
    result = write(
        tool,
        store,
        "joint",
        relation_contracts=[c.model_copy(update={"id": "has_feature", "name": "has feature"}).model_dump(mode="json") for c in legacy_relation_contracts() if c.id == "has_attribute"],
        upserts=[concept("a"), concept("b")],
        edge_operations=[{"operation": "create_edge", "edge": e} for e in edges],
    )
    assert result["status"] == "ok", result
    current = store.working
    assert {e.id for e in current.explicit_edges} == {e["id"] for e in edges}
    assert set(graph_diff(old, current)) == {
        "concept.a",
        "concept.b",
        "edge.hierarchy",
        "edge.related",
    }
    assert all(
        a.properties.targets[0] == ["column.orders.amount"]
        for a in current.semantic_assets
    )
    assert len([e for e in current.graph.edges if e.edge_type == "grounded_on"]) == 0
    assert all(e.sources[0].locator == "QA" for e in current.explicit_edges)
    groups = normalize_groups([], old, current)
    assert len(groups) == 1
    assert set(groups[0].member_ids) == set(graph_diff(old, current))
    with pytest.raises(ValueError, match="depends"):
        store.select(
            assessed_revision=current.revision,
            accepted_before=old.revision,
            member_ids=["edge.hierarchy"],
        )
    selected = store.select(
        assessed_revision=current.revision,
        accepted_before=old.revision,
        member_ids=graph_diff(old, current),
    )
    assert selected.explicit_edges == current.explicit_edges


def test_edge_only_edit_accept_restore_revert_delete_and_retain(case_graph):
    store, tool = seed_pair(case_graph)
    seed_accepted(store)
    before = store.accepted
    assert (
        write(
            tool,
            store,
            "edge-only",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    created = store.working
    assert graph_diff(before, created) == ["edge.hierarchy"]
    selected = store.select(
        assessed_revision=created.revision,
        accepted_before=before.revision,
        member_ids=["edge.hierarchy"],
    )
    store.restore(selected.revision, selected.revision)
    reopened = GraphStore(store.root, workspace=store.workspace, schema=store.schema)
    reopened.restore(selected.revision, selected.revision)
    assert (
        reopened.manager().get_edges(edge_type="is_a")[0].id == "edge.hierarchy"
    )
    assert (
        write(
            tool,
            store,
            "remove",
            edge_operations=[{"operation": "delete_edge", "edge_id": "edge.hierarchy"}],
        )["status"]
        == "ok"
    )
    assert graph_diff(store.accepted, store.working) == ["edge.hierarchy"]
    assert (
        write(tool, store, "revert-deletion", revert_ids=["edge.hierarchy"])["status"]
        == "ok"
    )
    assert store.working.explicit_edges == store.accepted.explicit_edges
    assert not graph_diff(store.accepted, store.working)
    changed = edge(target="concept.a", source="concept.b")
    assert (
        write(
            tool,
            store,
            "reverse",
            edge_operations=[{"operation": "update_edge", "edge": changed}],
        )["status"]
        == "ok"
    )
    assert (
        write(tool, store, "revert-change", revert_ids=["edge.hierarchy"])["status"]
        == "ok"
    )
    assert not graph_diff(store.accepted, store.working)
    assert (
        write(
            tool,
            store,
            "delete-again",
            edge_operations=[{"operation": "delete_edge", "edge_id": "edge.hierarchy"}],
        )["status"]
        == "ok"
    )
    deleted = store.select(
        assessed_revision=store.working_revision,
        accepted_before=store.accepted_revision,
        member_ids=["edge.hierarchy"],
    )
    store.restore(deleted.revision, deleted.revision)
    assert not store.retain().explicit_edges


@pytest.mark.parametrize(
    "bad",
    [
        edge(kind=" "),
        edge(target="concept.missing"),
        edge(kind="is_a", target="column.orders.amount"),
        edge(kind="related_to", relation_hint="depends_on"),
    ],
)
def test_invalid_relation_rolls_back_the_entire_agent_edit(case_graph, bad):
    store, tool = seed_pair(case_graph)
    old = store.working
    result = write(
        tool,
        store,
        "invalid",
        upserts=[concept("c")],
        edge_operations=[{"operation": "create_edge", "edge": bad}],
    )
    assert result["status"] == "rejected", result
    assert result["errors"][0]["hint"]
    assert store.working == old


def test_explicit_grounding_deletion_is_not_restored_and_business_edges_remain(case_graph):
    store, tool = seed_pair(case_graph)
    grounding = edge("binding", "grounded_on", target="column.orders.amount")
    assert write(tool, store, "explicit", edge_operations=[
        {"operation": "create_edge", "edge": edge()},
        {"operation": "create_edge", "edge": grounding},
    ])["status"] == "ok"
    assert write(tool, store, "remove-binding", edge_operations=[
        {"operation": "delete_edge", "edge_id": grounding["id"]},
    ])["status"] == "ok"
    assert not any(e.edge_type == "grounded_on" for e in store.working.graph.edges)
    replacement = concept("a")
    replacement["properties"]["targets"] = [["column.orders.region"]]
    assert write(tool, store, "update-node", upserts=[replacement])["status"] == "ok"
    assert {e.id for e in store.working.explicit_edges} == {"edge.hierarchy"}


def test_duplicate_fact_and_node_deletion_rejected_but_subclass_cycle_reviewed(case_graph):
    store, tool = seed_pair(case_graph)
    assert (
        write(
            tool,
            store,
            "edge",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    before = store.working_revision
    duplicate = write(
        tool,
        store,
        "duplicate",
        edge_operations=[{"operation": "create_edge", "edge": edge("duplicate")}],
    )
    assert duplicate["errors"][0]["code"] == "DUPLICATE_EDGE"
    cycle = write(
        tool,
        store,
        "cycle",
        edge_operations=[
            {
                "operation": "create_edge",
                "edge": edge("reverse", source="concept.b", target="concept.a"),
            }
        ],
    )
    assert cycle["status"] == "ok", cycle
    # Restore the original graph for the independent referential-integrity check.
    assert write(tool, store, "remove-reverse", edge_operations=[
        {"operation": "delete_edge", "edge_id": "edge.reverse"}
    ])["status"] == "ok"
    before = store.working_revision
    deletion = write(tool, store, "delete-node", delete_ids=["concept.a"])
    assert deletion["errors"][0]["code"] == "NODE_HAS_DEPENDENTS"
    assert store.working_revision == before
    result = write(
        tool,
        store,
        "remove-both",
        delete_ids=["concept.a"],
        edge_operations=[{"operation": "delete_edge", "edge_id": "edge.hierarchy"}],
    )
    assert result["status"] == "ok", result


def test_relation_contract_guides_and_diff_include_explicit_edges(case_graph):
    store, tool = seed_pair(case_graph)
    seed_accepted(store)
    assert (
        write(
            tool,
            store,
            "relation",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    tools = {t.name: t for t in build_tools(session_for(case_graph))}
    contract = ontology_contract(store.schema)
    hierarchy = next(r for r in contract["relations"] if r["type"] == "is_a")
    assert hierarchy["editable_from"] == ["concept", "event"]
    assert hierarchy["managed_from"] == []
    assert tools["inspect_graph"].invoke({"mode": "relations"})["relations"]
    diff = tools["inspect_graph"].invoke({"mode": "diff"})["result"]
    assert diff[0]["id"] == "edge.hierarchy"
    assert diff[0]["before"] is None
    guide = tools["inspect_graph"].invoke({"mode": "guide", "node_type": "concept"})
    assert "is_a" in guide["explicit_relation_types"]


def test_explicit_relations_survive_incoming_remap_merge_and_online_projection(
    case_graph,
):
    from cortex.capabilities.graph_merge.edges import normalize_edges
    from cortex.capabilities.online_context.output.projection import project_graph

    store, tool = seed_pair(case_graph)
    assert (
        write(
            tool,
            store,
            "relation",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    incoming = build_incoming_graph(
        store.schema,
        assets=store.working.physical_assets,
        cases=[("case", store.working.semantic_assets, store.working.explicit_edges)],
    )
    hierarchy = next(e for e in incoming.graph.edges if e.edge_type == "is_a")
    assert hierarchy.from_node_id != "concept.a"
    assert hierarchy.to_node_id != "concept.b"
    normalized = normalize_edges(
        existing=(),
        incoming=incoming.graph.edges,
        nodes=incoming.graph.nodes,
        node_mappings={},
        schema=store.schema,
    )
    merged, _ = GraphManager(store.schema).prepare_state(
        GraphState(
            graph=GraphSpace(nodes=incoming.graph.nodes, edges=normalized.edges),
            node_conflicts=(),
        )
    )
    projected, _ = project_graph(
        schema=store.schema,
        state=merged,
        selected_node_ids={hierarchy.from_node_id, hierarchy.to_node_id},
        choice_points=(),
        selected_by_point={},
    )
    assert hierarchy.id in {e.id for e in projected.edges}
    from cortex.capabilities.retrieve_db.closure import TypedGraphClosure
    from cortex.capabilities.retrieve_db.graph_index import GraphIndex

    retrieved = TypedGraphClosure().expand(
        GraphIndex.build(merged.graph), (hierarchy.from_node_id, hierarchy.to_node_id)
    )
    assert hierarchy.id in {e.id for e in retrieved.graph.edges}


def test_explicit_edge_edit_replay_and_storage_error_do_not_advance_state(
    case_graph, monkeypatch
):
    import cortex.capabilities.sql_training.graph.snapshots as module

    store, tool = seed_pair(case_graph)
    request = {
        "edit_id": "edge-replay",
        "expected_revision": store.working_revision,
        "edge_operations": [{"operation": "create_edge", "edge": edge()}],
    }
    first = tool.invoke(request)
    assert first["status"] == "ok", first
    assert tool.invoke(request)["applied_revision"] == first["applied_revision"]
    old = store.working_revision
    monkeypatch.setattr(
        module,
        "write_json_atomic",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = write(
        tool,
        store,
        "failed-write",
        edge_operations=[{"operation": "delete_edge", "edge_id": "edge.hierarchy"}],
    )
    assert result["status"] == "error"
    assert store.working_revision == old
    assert store.working.explicit_edges[0].id == "edge.hierarchy"


def test_complete_reverse_join_key_rejected_but_different_key_allowed():
    from test_graph_manager import apply, create, node

    from cortex.schema import load_root_schema

    manager = GraphManager(load_root_schema())
    assert (
        apply(
            manager,
            create(node("t1", "table", {"table_name": "first"})),
            create(node("t2", "table", {"table_name": "second"})),
            *[
                create(
                    node(
                        f"c{t}{c}",
                        "column",
                        {"table_ref": f"t{t}", "column_name": f"col{c}"},
                    )
                )
                for t in (1, 2)
                for c in (1, 2)
            ],
        ).status
        == "applied"
    )

    def join(name, reverse=False, pairs=(1, 2)):
        a, b = (2, 1) if reverse else (1, 2)
        return {
            "operation": "create_edge",
            "edge": edge(
                name,
                "joins_with",
                f"t{a}",
                f"t{b}",
                column_pairs=[
                    {"from_column_ref": f"c{a}{c}", "to_column_ref": f"c{b}{c}"}
                    for c in pairs
                ],
            ),
        }

    assert apply(manager, join("join")).status == "applied"
    result = apply(manager, join("reverse", reverse=True, pairs=(2, 1)))
    assert result.status == "rejected"
    assert result.errors[0].code == "DUPLICATE_EDGE"
    assert apply(manager, join("different", pairs=(1,))).status == "applied"


def test_assessor_receives_edge_changes_and_current_relation_schema(case_graph):
    import json

    from test_agent_runtime import ScriptModel

    from cortex.capabilities.sql_training.training.feedback import assess

    store, tool = seed_pair(case_graph)
    seed_accepted(store)
    old = store.accepted
    assert (
        write(
            tool,
            store,
            "relation",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    groups = normalize_groups([], old, store.working)
    from test_agent_runtime import assessment_reply
    model = ScriptModel([assessment_reply])
    assess(
        model,
        record={},
        schema=store.schema,
        baseline=old,
        accepted=old,
        trial=store.working,
        groups=groups,
        targets=[],
        validation={},
        context_limit=64000,
    )
    payload = json.loads(model._calls[0][1].content)
    assert [u["unit_id"] for u in payload["units"]] == ["edge.hierarchy"]
    assert payload["units"][0]["candidate"]["type"] == "is_a"
    assert payload["units"][0]["candidate"]["properties"]["description"]
    assert all(e["id"] != "edge.hierarchy" for e in payload["related_knowledge"]["edges"])
    assert "is_a" in {r["type"] for r in payload["related_knowledge"]["relation_definitions"]}


def test_generation_preserves_explicit_grounding_without_inferred_duplicate(case_graph):
    from cortex.capabilities.sql_training.graph.nodes import build_graph
    from cortex.schema import GraphEdge
    store, _ = seed_pair(case_graph)
    ground = GraphEdge.model_validate(edge("binding", "grounded_on", target="column.orders.amount"))
    graph = build_graph(store.working.assets, store.working.catalog_edges, store.schema, explicit_edges=[ground])
    assert [e for e in graph.edges if e.edge_type == "grounded_on"] == [ground]


def test_partial_node_acceptance_keeps_unchanged_relations(case_graph):
    store, tool = seed_pair(case_graph)
    assert (
        write(
            tool,
            store,
            "relation",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    seed_accepted(store)
    before = store.accepted_revision
    a, b = concept("a"), concept("b")
    a["description"], b["description"] = "New a", "New b"
    assert write(tool, store, "change-both", upserts=[a, b])["status"] == "ok"
    selected = store.select(
        assessed_revision=store.working_revision,
        accepted_before=before,
        member_ids=["concept.a"],
    )
    assert selected.explicit_edges == store.accepted.explicit_edges
    assert (
        next(n for n in selected.semantic_assets if n.id == "concept.b").description
        != "New b"
    )


def test_revert_new_relation_and_invalid_replay_changes_nothing(case_graph):
    store, tool = seed_pair(case_graph)
    seed_accepted(store)
    assert (
        write(
            tool,
            store,
            "add",
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    assert (
        write(tool, store, "revert-new", revert_ids=["edge.hierarchy"])["status"]
        == "ok"
    )
    assert not graph_diff(store.accepted, store.working)
    before = store.working_revision
    result = write(
        tool,
        store,
        "add",
        edge_operations=[{"operation": "create_edge", "edge": edge("other")}],
    )
    assert result["status"] == "rejected"
    assert store.working_revision == before


def test_schema_extension_is_writable_without_new_derivation_code(case_graph, tmp_path):
    original, database, workspace = case_graph
    template = original.schema.edge_type_index()["is_a"]
    schema = original.schema.model_copy(
        update={
            "edge_types": (
                *original.schema.edge_types,
                template.model_copy(
                    update={
                        "id": "summarizes",
                        "description": "Source summarizes the target concept.",
                    }
                ),
            )
        }
    )
    store = GraphStore(tmp_path / "extended", workspace=workspace, schema=schema)
    store.initialize(original.working.physical_assets, original.working.catalog_edges)
    tool = edit_tool((store, database, workspace))
    result = write(
        tool,
        store,
        "extended",
        upserts=[concept("a"), concept("b")],
        edge_operations=[{"operation": "create_edge", "edge": edge(kind="summarizes")}],
    )
    assert result["status"] == "ok", result
    assert store.manager().get_edges(edge_type="summarizes")[0].id == "edge.hierarchy"
    contract = ontology_contract(schema)
    relation = next(r for r in contract["relations"] if r["type"] == "summarizes")
    assert relation["editable_from"] == ["concept", "event"]
    assert relation["description"] == "Source summarizes the target concept."


def test_delete_edge_shape_error_guides_a_successful_retry(case_graph):
    store, tool = seed_pair(case_graph)
    assert write(tool, store, 'add-edge', edge_operations=[
        {'operation': 'create_edge', 'edge': edge()},
    ])['status'] == 'ok'
    revision = store.working_revision
    result = write(tool, store, 'wrong-delete', edge_operations=[
        {'operation': 'delete_edge', 'edge': edge()},
    ])
    assert result['status'] == 'rejected'
    assert store.working_revision == revision
    assert result['errors'][0]['path'] == 'edge_operations.0'
    assert '"operation":"delete_edge","edge_id"' in result['errors'][0]['hint']
    assert write(tool, store, 'correct-delete', edge_operations=[
        {'operation': 'delete_edge', 'edge_id': edge()['id']},
    ])['status'] == 'ok'
    assert not store.working.explicit_edges


def test_learning_rejects_abstract_attribute_edge_without_changing_graph(case_graph):
    store, _, _ = case_graph
    before = store.working
    result = write(edit_tool(case_graph), store, 'abstract', upserts=[concept('a'), concept('b')], edge_operations=[{'operation': 'create_edge', 'edge': edge('abstract', 'has_attribute')}])
    assert result['status'] == 'rejected'
    assert 'abstract relation base' in str(result)
    assert store.working == before
