"""Conflict edits share the ordinary graph transaction and Agent receipt paths."""

import json

import pytest
from relation_fixtures import metric_context_contract
from test_explicit_relations import concept, edit_tool, write
from test_graph_manager import apply, create, metric, node

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.sql_training.graph.snapshots import graph_diff
from cortex.schema import (
    GraphNode,
    NodeContent,
    load_root_schema,
)


def set_operation(manager, key="m", refs=("a", "b")):
    base = GraphNode.model_validate(metric(key, refs[0]))
    contents = [
        NodeContent.model_validate(
            {
                k: getattr(GraphNode.model_validate(metric(key, ref)), k)
                for k in NodeContent.model_fields
            }
        )
        for ref in refs
    ]
    owner, conflict = manager.compact_conflict(
        base, [("first", contents[0], ()), ("second", contents[1], ())]
    )
    return {
        "operation": "set_conflict",
        "node": owner.model_dump(mode="json"),
        "conflict": conflict.model_dump(mode="json"),
    }


@pytest.fixture
def manager():
    manager = GraphManager(load_root_schema())
    assert (
        apply(
            manager,
            create(node("t", "table", {"table_name": "orders"})),
            *[
                create(node(key, "column", {"table_ref": "t", "column_name": key}))
                for key in ("a", "b", "c")
            ],
            relation_contracts=(metric_context_contract(), metric_context_contract("context")),
        ).status
        == "applied"
    )
    return manager


def test_set_and_resolve_new_conflict_atomically(manager):
    request = set_operation(manager)
    result = apply(manager, request)
    assert result.status == "applied", result.errors
    assert result.changed_node_ids == ["m"]
    assert {tuple(e.properties["alternative_ids"]) for e in manager.get_edges("m")} == {
        ("first",),
        ("second",),
    }
    result = apply(
        manager,
        {"operation": "resolve_conflict", "node_id": "m", "alternative_id": "second"},
    )
    assert result.status == "applied", result.errors
    assert manager.get_conflict("m") is None
    assert manager.get_node("m").properties["expression"]["refs"] == {"x": "b"}
    assert [e.to_node_id for e in manager.get_edges("m")] == ["b"]
    assert manager.get_edges("m")[0].properties["description"]


def test_normal_node_can_become_conflicted_and_replaced(manager):
    assert apply(manager, create(metric("m", "a"))).status == "applied"
    assert apply(manager, set_operation(manager)).status == "applied"
    updated = set_operation(manager, refs=("b", "c"))
    assert apply(manager, updated).status == "applied"
    assert {e.to_node_id for e in manager.get_edges("m")} == {"b", "c"}
    manager.validate_state(manager.get_snapshot())


def test_resolution_keeps_selected_explicit_and_shared_independent_facts(manager):
    request = set_operation(manager)
    assert (
        apply(
            manager,
            request,
            {
                "operation": "create_edge",
                "edge": {
                    "id": "custom",
                    "edge_type": "custom_measures",
                    "from_node_id": "m",
                    "to_node_id": "b",
                    "properties": {"alternative_ids": ["second"]},
                },
            },
            {
                "operation": "create_edge",
                "edge": {
                    "id": "shared",
                    "edge_type": "context",
                    "from_node_id": "m",
                    "to_node_id": "c",
                },
            },
        ).status
        == "applied"
    )
    assert (
        apply(
            manager,
            {
                "operation": "resolve_conflict",
                "node_id": "m",
                "alternative_id": "second",
            },
        ).status
        == "applied"
    )
    assert {e.id for e in manager.get_edges("m")} == {"custom", "shared", "fixture.m.grounded_on.b"}
    assert all("alternative_ids" not in e.properties for e in manager.get_edges("m"))
    assert (
        manager.get_edges("m", edge_type="custom_measures")[0].reference_origin is None
    )


@pytest.mark.parametrize("bad", ["reference", "owner", "candidate", "type"])
def test_bad_conflicts_reject_the_whole_batch(manager, bad):
    before = manager.get_snapshot()
    operation = set_operation(manager)
    if bad == "reference":
        operation["conflict"]["alternatives"][0]["overrides"][
            "properties.references"
        ] = ["missing"]
    if bad == "owner":
        operation["conflict"]["node_id"] = "another"
    if bad == "candidate":
        operation["conflict"]["alternatives"][0]["overrides"][
            "properties.expression"
        ] = {"expr": ""}
    if bad == "type":
        operation["node"]["node_type"] = "event"
    result = apply(manager, create(metric("good", "a")), operation)
    assert result.status == "rejected"
    assert result.errors[0].hint
    assert manager.get_snapshot() == before


def test_unknown_resolution_duplicate_owner_permissions_and_revision(manager):
    assert apply(manager, set_operation(manager)).status == "applied"
    before = manager.get_snapshot()
    assert (
        apply(
            manager,
            {
                "operation": "resolve_conflict",
                "node_id": "m",
                "alternative_id": "missing",
            },
        )
        .errors[0]
        .code
        == "ALTERNATIVE_NOT_FOUND"
    )
    assert (
        apply(
            manager,
            {
                "operation": "resolve_conflict",
                "node_id": "a",
                "alternative_id": "first",
            },
        )
        .errors[0]
        .code
        == "CONFLICT_NOT_FOUND"
    )
    assert (
        apply(
            manager,
            set_operation(manager),
            {
                "operation": "resolve_conflict",
                "node_id": "m",
                "alternative_id": "first",
            },
        ).status
        == "rejected"
    )
    assert (
        apply(
            manager,
            {
                "operation": "resolve_conflict",
                "node_id": "m",
                "alternative_id": "first",
            },
            allowed_node_types=frozenset({"concept"}),
        )
        .errors[0]
        .code
        == "OPERATION_NOT_ALLOWED"
    )
    assert (
        manager.apply(
            [set_operation(manager)], expected_revision="stale", request_id="stale"
        )
        .errors[0]
        .code
        == "REVISION_MISMATCH"
    )
    assert manager.get_snapshot() == before


def test_idempotency_and_commit_failure_keep_one_transaction(manager):
    operation = set_operation(manager)
    revision = manager.revision
    calls = []

    def commit(state, expected):
        calls.append((state, expected))
        return "saved"

    from test_graph_manager import authored_operations
    complete_operations = authored_operations(manager, [operation])
    first = manager.apply(complete_operations, expected_revision=revision, request_id="once", commit=commit)
    second = manager.apply(complete_operations, expected_revision=revision, request_id="once", commit=commit)
    assert first.status == second.status == "applied" and len(calls) == 1
    before = manager.get_snapshot()

    def broken(*args):
        raise OSError("no storage")

    result = manager.apply(
        [{"operation": "resolve_conflict", "node_id": "m", "alternative_id": "first"}],
        expected_revision="saved",
        request_id="failed",
        commit=broken,
    )
    assert (
        result.status == "error"
        and manager.get_snapshot() == before
        and manager.revision == "saved"
    )


def agent_operation(store):
    operation = set_operation(
        store.manager(),
        key="metric.sales",
        refs=("column.orders.amount", "column.orders.region"),
    )
    # Agent does not write provenance or the conflict marker.
    operation["node"]["properties"]["pattern"] = "Reusable recorded quantity"
    operation["node"]["properties"]["sql_generation_plans"] = [
        {
            "id": "main",
            "application": "When the selected recorded quantity is requested",
            "steps": ["Read the selected operand and evaluate its formula"],
        }
    ]
    operation["node"].pop("sources")
    operation["node"].pop("merge_status")
    for field in (
        "consolidation_status",
        "preservation_reasons",
        "alternatives",
        "conflict_points",
    ):
        operation["node"].pop(field, None)
    for alternative in operation["conflict"]["alternatives"]:
        alternative.pop("sources")
    return operation


def agent_formula_edges(store, request):
    from test_graph_manager import authored_operations
    result = []
    for operation in authored_operations(store.manager(), [request]):
        if operation.get("operation") not in {"create_edge", "update_edge"}:
            continue
        edge = operation["edge"]
        edge.pop("sources", None)
        edge.get("properties", {}).pop("alternative_ids", None)
        result.append(operation)
    return result


def test_agent_write_read_resolve_and_reopen(case_graph):
    from cortex.capabilities.sql_training.graph.snapshots import GraphStore, graph_diff

    store, _, workspace = case_graph
    tool = edit_tool(case_graph)
    original = store.working
    request = agent_operation(store)
    first = write(tool, store, "conflict", conflict_operations=[request])
    assert first["status"] == "ok", "\n".join(
        e["message"] for e in first.get("errors", [])
    )
    assert "metric.sales" in graph_diff(original, store.working)
    assert not any(a.id == "metric.sales" for a in store.working.assets)
    assert store.manager().get_conflict("metric.sales")
    reopened = GraphStore(store.root, workspace=workspace, schema=store.schema)
    reopened.restore(store.working_revision, store.accepted_revision)
    assert reopened.manager().get_conflict("metric.sales")
    persisted = json.loads((store.root / f"{store.working_revision}.json").read_text())
    assert "node_conflicts" not in persisted
    stored_owner = next(
        n for n in persisted["graph"]["nodes"] if n["id"] == "metric.sales"
    )
    assert len(stored_owner["alternatives"]) == 2
    assert all(a["sources"] for a in stored_owner["alternatives"])
    result = write(
        tool,
        store,
        "resolve",
        conflict_operations=[
            {
                "operation": "resolve_conflict",
                "node_id": "metric.sales",
                "alternative_id": "first",
            }
        ],
    )
    assert result["status"] == "ok", result
    assert store.manager().get_conflict("metric.sales") is None
    assert any(a.id == "metric.sales" for a in store.working.semantic_assets)
    assert store.manager().get_node("metric.sales").properties["expression"]["refs"] == {"x": "column.orders.amount"}


def test_agent_conflict_acceptance_and_discard(case_graph):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    assert (
        write(tool, store, "conflict", conflict_operations=[agent_operation(store)])[
            "status"
        ]
        == "ok"
    )
    trial = store.working_revision
    accepted = store.select(
        assessed_revision=trial,
        accepted_before=store.accepted_revision,
        member_ids=graph_diff(store.accepted, store.working),
    )
    store.accepted_revision = accepted.revision
    store.working_revision = accepted.revision
    assert store.accepted.conflict_nodes
    assert (
        write(
            tool,
            store,
            "resolve",
            conflict_operations=[
                {
                    "operation": "resolve_conflict",
                    "node_id": "metric.sales",
                    "alternative_id": "second",
                }
            ],
        )["status"]
        == "ok"
    )
    store.retain()
    assert store.manager().get_conflict("metric.sales")


def test_replacing_candidates_never_moves_an_explicit_fact_to_another_branch(manager):
    assert (
        apply(
            manager,
            set_operation(manager),
            {
                "operation": "create_edge",
                "edge": {
                    "id": "only_first",
                    "edge_type": "custom_measures",
                    "from_node_id": "m",
                    "to_node_id": "a",
                    "properties": {"alternative_ids": ["first"]},
                },
            },
        ).status
        == "applied"
    )
    replacement = set_operation(manager, refs=("b", "a"))
    assert apply(manager, replacement).status == "applied"
    assert next(e for e in manager.get_edges("m") if e.id == "only_first").properties["alternative_ids"] == ["first"]
    assert {
        (e.to_node_id, tuple(e.properties["alternative_ids"]))
        for e in manager.get_edges("m") if e.edge_type == "grounded_on"
    } == {("b", ("first",)), ("a", ("second",))}


def test_dropped_branch_independent_facts_are_removed_on_resolution(manager):
    assert (
        apply(
            manager,
            set_operation(manager),
            {
                "operation": "create_edge",
                "edge": {
                    "id": "discard",
                    "edge_type": "context",
                    "from_node_id": "m",
                    "to_node_id": "c",
                    "properties": {"alternative_ids": ["second"]},
                },
            },
        ).status
        == "applied"
    )
    assert (
        apply(
            manager,
            {
                "operation": "resolve_conflict",
                "node_id": "m",
                "alternative_id": "first",
            },
        ).status
        == "applied"
    )
    assert not any(e.id == "discard" for e in manager.get_edges("m"))


def test_agent_receipt_replay_and_invalid_batch_do_not_publish_twice(case_graph):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    request = agent_operation(store)
    revision = store.working_revision
    payload = {
        "edit_id": "same",
        "expected_revision": revision,
        "conflict_operations": [request],
        "edge_operations": agent_formula_edges(store, request),
    }
    first = tool.invoke(payload)
    assert first["status"] == "ok", first
    count = len(list(store.root.glob("*.json")))
    assert tool.invoke(payload)["applied_revision"] == first["applied_revision"]
    assert len(list(store.root.glob("*.json"))) == count
    before = store.working_revision
    bad = write(
        tool,
        store,
        "bad",
        conflict_operations=[
            {
                "operation": "resolve_conflict",
                "node_id": "metric.sales",
                "alternative_id": "first",
            }
        ],
        edge_operations=[
            {
                "operation": "create_edge",
                "edge": {
                    "id": "bad",
                    "edge_type": "context",
                    "from_node_id": "metric.sales",
                    "to_node_id": "missing",
                },
            }
        ],
    )
    assert bad["status"] == "rejected"
    assert store.working_revision == before
    assert store.manager().get_conflict("metric.sales")


@pytest.mark.parametrize("metadata", ["node_sources", "marker", "alternative_sources"])
def test_agent_cannot_supply_conflict_provenance_or_markers(case_graph, metadata):
    store, _, _ = case_graph
    request = agent_operation(store)
    if metadata == "node_sources":
        request["node"]["sources"] = []
    if metadata == "marker":
        request["node"]["merge_status"] = "conflict"
    if metadata == "alternative_sources":
        request["conflict"]["alternatives"][0]["sources"] = []
    before = store.working_revision
    result = write(
        edit_tool(case_graph), store, "invalid", conflict_operations=[request]
    )
    assert result["status"] == "rejected" and "code-managed" in str(result)
    assert store.working_revision == before


def test_agent_inspection_and_normal_edits_preserve_compact_conflicts(case_graph):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    store, _, _ = case_graph
    tools = build_tools(session_for(case_graph))
    tool = next(t for t in tools if t.name == "edit_graph")
    assert (
        write(tool, store, "set", conflict_operations=[agent_operation(store)])[
            "status"
        ]
        == "ok"
    )
    inspector = next(t for t in tools if t.name == "inspect_graph")
    result = inspector.invoke({"mode": "node", "node_id": "metric.sales"})["result"]
    assert "conflict" not in result
    assert "overrides" in result["node"]["alternatives"][0]
    assert write(tool, store, "other", upserts=[concept("other")])["status"] == "ok"
    assert store.manager().get_conflict("metric.sales")
    result = inspector.invoke({"mode": "diff"})["result"]
    assert any(
        row["id"] == "metric.sales" and row["after"]["node"]["alternatives"]
        for row in result
    )


def test_resolving_shared_default_fact_keeps_only_selected_candidate_provenance(
    manager,
):
    from cortex.schema import SourceReference

    base = GraphNode.model_validate(metric("m", "a"))
    content = {key: getattr(base, key) for key in NodeContent.model_fields}
    owner, conflict = manager.compact_conflict(
        base,
        [
            (
                key,
                NodeContent.model_validate({**content, "description": key}),
                (SourceReference(source_id=key),),
            )
            for key in ("first", "second")
        ],
    )
    assert (
        apply(
            manager,
            {
                "operation": "set_conflict",
                "node": owner.model_dump(mode="json"),
                "conflict": conflict.model_dump(mode="json"),
            },
        ).status
        == "applied"
    )
    assert len(manager.get_edges("m")) == 1
    assert (
        apply(
            manager,
            {
                "operation": "resolve_conflict",
                "node_id": "m",
                "alternative_id": "first",
            },
        ).status
        == "applied"
    )
    assert {s.source_id for s in manager.get_edges("m")[0].sources} == {"first", "second"}


def test_agent_conflict_rule_does_not_bypass_direct_source_trigger_check(case_graph):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    owner = GraphNode(
        id="generation_rule.r",
        node_type="generation_rule",
        name="r",
        properties={
            "condition": "Count requested",
            "action": "Count rows",
            "reason": "Return row count",
            "triggers": {
                "question": {"phrases": ["Invented phrase not in the source question"]}
            },
        },
    )
    contents = [
        NodeContent.model_validate(
            {
                **{key: getattr(owner, key) for key in NodeContent.model_fields},
                "description": key,
            }
        )
        for key in ("first", "second")
    ]
    owner, conflict = store.manager().compact_conflict(
        owner,
        [(key, c, ()) for key, c in zip(("first", "second"), contents, strict=True)],
    )
    operation = {
        "operation": "set_conflict",
        "node": owner.model_dump(
            mode="json",
            exclude={
                "sources",
                "merge_status",
                "consolidation_status",
                "preservation_reasons",
                "alternatives",
                "conflict_points",
            },
        ),
        "conflict": conflict.model_dump(mode="json"),
    }
    for alternative in operation["conflict"]["alternatives"]:
        alternative.pop("sources")
    result = write(tool, store, "invalid_rule", conflict_operations=[operation])
    assert (
        result["status"] == "rejected"
        and result["errors"][0]["code"] == "QUESTION_TRIGGER_NOT_IN_SOURCE"
    )
    assert result["errors"][0]["path"].startswith("conflict_operations.")


def test_conflict_write_schema_hides_code_managed_fields():
    from cortex.capabilities.sql_training.learning.inputs import (
        conflict_operation_schema,
    )

    schema = conflict_operation_schema()
    assert set(schema["properties"]["operation"]["enum"]) == {
        "set_conflict",
        "resolve_conflict",
    }
    assert (
        not {"sources", "merge_status"}
        & schema["properties"]["node"]["properties"].keys()
    )
    assert (
        "sources"
        not in schema["properties"]["conflict"]["properties"]["alternatives"]["items"][
            "properties"
        ]
    )


def test_case_export_requires_explicit_resolution_instead_of_losing_candidates(
    case_graph, tmp_path
):
    from cortex.capabilities.sql_training.artifacts.cases import (
        load_case_assets,
        seal_case,
    )

    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    assert (
        write(tool, store, "set", conflict_operations=[agent_operation(store)])[
            "status"
        ]
        == "ok"
    )
    accepted = store.select(
        assessed_revision=store.working_revision,
        accepted_before=store.accepted_revision,
        member_ids=graph_diff(store.accepted, store.working),
    )
    store.accepted_revision = store.working_revision = accepted.revision
    with pytest.raises(ValueError, match="Resolve accepted conflicts"):
        seal_case(tmp_path / "case", {}, store)
    assert not (tmp_path / "case" / "result").exists()
    assert (
        write(
            tool,
            store,
            "resolve",
            conflict_operations=[
                {
                    "operation": "resolve_conflict",
                    "node_id": "metric.sales",
                    "alternative_id": "first",
                }
            ],
        )["status"]
        == "ok"
    )
    accepted = store.select(
        assessed_revision=store.working_revision,
        accepted_before=store.accepted_revision,
        member_ids=graph_diff(store.accepted, store.working),
    )
    store.accepted_revision = store.working_revision = accepted.revision
    from cortex.capabilities.sql_training.graph.snapshots import fingerprint

    result = {
        "final_ontology_fingerprint": fingerprint(
            [a.model_dump(mode="json") for a in store.accepted.semantic_assets]
        )
    }
    seal_case(tmp_path / "case", result, store)
    assert [a.id for a in load_case_assets(result, case_root=tmp_path / "case")] == [
        "metric.sales"
    ]


def test_resolve_and_delete_losing_dependency_in_one_batch(manager):
    assert apply(manager, set_operation(manager)).status == "applied"
    result = apply(
        manager,
        {"operation": "resolve_conflict", "node_id": "m", "alternative_id": "second"},
        {"operation": "delete_node", "node_id": "a"},
    )
    assert result.status == "applied", result.errors
    assert manager.get_node("a") is None and manager.get_node("m").properties["expression"]["refs"] == {"x": "b"}


def test_equivalent_scoped_facts_coalesce_when_a_candidate_is_selected(manager):
    from cortex.schema import GraphEdge, GraphSpace, SourceReference

    assert apply(manager, set_operation(manager)).status == "applied"
    state = manager.get_snapshot()
    extra = tuple(
        GraphEdge(
            id=f"context_{index}",
            edge_type="context",
            from_node_id="m",
            to_node_id="c",
            properties={"alternative_ids": ids, "description": "A shared explicit context fact."},
            sources=(SourceReference(source_id=str(index)),),
        )
        for index, ids in enumerate((["first"], ["first", "second"]))
    )
    state = state.model_copy(
        update={
            "graph": GraphSpace(
                nodes=state.graph.nodes, edges=(*state.graph.edges, *extra)
            )
        }
    )
    reader = GraphManager(manager.schema, state)
    result = apply(
        reader,
        {"operation": "resolve_conflict", "node_id": "m", "alternative_id": "first"},
    )
    assert result.status == "applied", result.errors
    facts = reader.get_edges("m", edge_type="context")
    assert len(facts) == 1 and {s.source_id for s in facts[0].sources} == {"0", "1"}


def test_agent_storage_failure_does_not_advance_heads_or_cache_success(
    case_graph, monkeypatch
):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    before = store.working_revision
    request = agent_operation(store)
    payload = {
        "edit_id": "retryable",
        "expected_revision": before,
        "conflict_operations": [request],
        "edge_operations": agent_formula_edges(store, request),
    }
    save = store.save

    def failed(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "save", failed)
    result = tool.invoke(payload)
    assert result["status"] == "error"
    assert (
        store.working_revision == before
        and store.manager().get_conflict("metric.sales") is None
    )
    monkeypatch.setattr(store, "save", save)
    assert tool.invoke(payload)["status"] == "ok"


def test_conflict_metadata_is_owned_and_resolved_atomically(manager):
    operation = set_operation(manager)
    result = apply(manager, operation)
    assert result.status == "applied", result.errors
    snapshot = manager.get_snapshot()
    assert set(type(snapshot).model_fields) == {"graph"}
    owner = manager.get_conflict("m")
    stored = next(node for node in snapshot.graph.nodes if node.id == "m")
    assert owner == stored
    assert owner.alternatives and owner.consolidation_status == "unresolved"
    assert "node_conflicts" not in snapshot.model_dump(mode="json")
    result = apply(
        manager,
        {"operation": "resolve_conflict", "node_id": "m", "alternative_id": "first"},
    )
    assert result.status == "applied", result.errors
    resolved = manager.get_node("m")
    assert resolved.merge_status == "normal"
    assert not resolved.alternatives and not resolved.conflict_points
    assert not resolved.preservation_reasons and resolved.consolidation_status is None


def test_set_conflict_accepts_one_complete_inline_node(manager):
    node = set_operation(manager)["node"]
    result = apply(manager, {"operation": "set_conflict", "node": node})
    assert result.status == "applied", result.errors
    owner = manager.get_conflict("m")
    assert owner.id == node["id"]
    assert [a.alternative_id for a in owner.alternatives] == ["first", "second"]
    assert "node_conflicts" not in manager.get_snapshot().model_dump(mode="json")
