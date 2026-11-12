"""The public graph boundary: atomic batches, actionable errors and actual IDs."""

import pytest

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphEdge, load_root_schema
from cortex.schema.references import remap_references


def node(node_id, kind, properties):
    return {"id": node_id, "node_type": kind, "name": node_id, "properties": properties}


def metric(node_id, reference):
    return node(
        node_id,
        "metric",
        {
            "expression": {"expr": "SUM(${x})", "refs": {"x": reference}},
        },
    )


def create(value):
    return {"operation": "create_node", "node": value}


def authored_operations(manager, operations):
    """Test authors provide complete formula edges, including conflict branches."""
    from cortex.schema import GraphNode, NodeMergeConflict

    operations = [dict(op) for op in operations]
    present = {n.id: n.node_type for n in manager.get_snapshot().graph.nodes}
    present.update({op["node"]["id"]: op["node"]["node_type"] for op in operations
                    if isinstance(op.get("node"), dict)})
    desired = {}
    changed_owners = set()
    for op in operations:
        if op.get("operation") == "delete_node":
            changed_owners.add(op["node_id"])
            continue
        if op.get("operation") not in {"create_node", "update_node", "set_conflict"}:
            continue
        raw = op.get("node", {})
        if raw.get("node_type") != "metric":
            continue
        owner = raw["id"]
        changed_owners.add(owner)
        record = GraphNode.model_validate(raw)
        if op.get("operation") == "set_conflict" and not record.alternatives and op.get("conflict"):
            conflict = NodeMergeConflict.model_validate(op["conflict"])
            record = record.model_copy(update={"alternatives": conflict.alternatives, "merge_status": "conflict"})
        variants = [(record.properties, None, record.sources)]
        if record.alternatives:
            variants = [(manager.resolve_alternative(record, alt).properties, alt.alternative_id, alt.sources)
                        for alt in record.alternatives]
        for properties, alternative, sources in variants:
            expression = properties.get("expression", {})
            refs = expression.get("refs", {}).values() if isinstance(expression, dict) else ()
            for ref in set(refs):
                kind = "depends_on" if present.get(ref) in {"concept", "metric"} else "grounded_on"
                key = f"fixture.{owner}.{kind}.{ref}"
                value = desired.setdefault(key, {
                    "id": key, "edge_type": kind, "from_node_id": owner,
                    "to_node_id": ref, "properties": {"description": "Explicit fixture operand connection."},
                })
                if alternative:
                    value["properties"].setdefault("alternative_ids", []).append(alternative)
                if sources:
                    value.setdefault("sources", [])
                    for source in sources:
                        item = source.model_dump(mode="json")
                        if item not in value["sources"]:
                            value["sources"].append(item)
    existing = {edge.id: edge for edge in manager.get_edges()}
    explicit_ids = {op.get("edge", {}).get("id") or op.get("edge_id") for op in operations}
    for key, value in existing.items():
        if key.startswith("fixture.") and value.from_node_id in changed_owners and key not in desired and key not in explicit_ids:
            operations.append({"operation": "delete_edge", "edge_id": key})
    for key, value in desired.items():
        if key in explicit_ids:
            continue
        if any(op.get("edge", {}).get("from_node_id") == value["from_node_id"]
               and op["edge"].get("to_node_id") == value["to_node_id"]
               and op["edge"].get("edge_type") == value["edge_type"]
               for op in operations):
            continue
        if key not in existing or existing[key].model_dump(mode="json")["properties"] != value["properties"]:
            operations.append({"operation": "update_edge" if key in existing else "create_edge", "edge": value})
    # Tests unrelated to description author the invariant here; explicit invalid
    # description tests pass an empty value and are not silently repaired.
    for op in operations:
        if isinstance(op.get("edge"), dict):
            op["edge"] = dict(op["edge"])
            props = dict(op["edge"].get("properties", {}))
            props.setdefault("description", "Explicit fixture relationship.")
            op["edge"]["properties"] = props
    return operations


def apply(manager, *operations, request_id=None, **kwargs):
    return manager.apply(
        authored_operations(manager, operations),
        expected_revision=manager.revision,
        request_id=request_id or f"request-{manager.revision}",
        **kwargs,
    )


@pytest.fixture
def manager():
    manager = GraphManager(load_root_schema())
    result = apply(
        manager,
        create(
            node(
                "field",
                "column",
                {"table_ref": "table", "column_name": "amount", "data_type": "REAL"},
            )
        ),
        create(node("table", "table", {"table_name": "orders"})),
    )
    assert result.status == "applied", result.errors
    return manager


def test_batch_forward_refs_use_actual_types_and_create_edges(manager):
    result = apply(manager, create(metric("b", "a")), create(metric("a", "field")))
    assert result.status == "applied", result.errors
    assert {
        (e.edge_type, e.from_node_id, e.to_node_id) for e in manager.get_edges()
    } == {
        ("belongs_to", "field", "table"),
        ("depends_on", "b", "a"),
        ("grounded_on", "a", "field"),
    }
    assert manager.get_dependencies("b") == ["a"]


def test_reviewed_reconciliation_replaces_conflict_and_retains_explicit_facts(manager):
    from test_conflict_writes import set_operation

    from cortex.schema.models import EdgeType

    contract = EdgeType(id="context", name="Context", description="A metric contextual association, not a formula input.",
                        from_node_types=["metric"], to_node_types=["metric"],
                        direction_semantics="Source metric has contextual relation to target; no inference.",
                        evidence_requirement="An explicit business explanation is required.")

    assert (
        apply(
            manager,
            create(metric("a", "field")),
            create(metric("b", "field")),
            set_operation(manager),
            {
                "operation": "create_edge",
                "edge": {
                    "id": "explicit",
                    "edge_type": "context",
                    "from_node_id": "m",
                    "to_node_id": "a",
                    "properties": {"alternative_ids": ["first"]},
                },
            },
            relation_contracts=(contract,),
        ).status
        == "applied"
    )
    replacement = {"operation": "update_node", "node": metric("m", "field")}
    before = manager.get_snapshot()
    rejected = apply(manager, replacement, request_id="ordinary-update")
    assert rejected.errors[0].code == "CONFLICT_REQUIRES_RECONCILIATION"
    assert manager.get_snapshot() == before
    replacement["node"]["properties"] = {
        "expression": {"expr": "${x} + ${y}", "refs": {"x": "field", "y": "a"}},
    }
    result = apply(
        manager, replacement, request_id="reviewed-update", reviewed_reconciliation=True
    )
    assert result.status == "applied", result.errors
    assert manager.get_conflict("m") is None
    assert {(edge.edge_type, edge.to_node_id) for edge in manager.get_edges("m")} == {
        ("grounded_on", "field"),
        ("depends_on", "a"),
    }
    assert all(
        "alternative_ids" not in edge.properties for edge in manager.get_edges("m")
    )


def test_reviewed_reconciliation_invalid_replacement_is_atomic(manager):
    from test_conflict_writes import set_operation

    assert (
        apply(
            manager,
            create(metric("a", "field")),
            create(metric("b", "field")),
            set_operation(manager),
        ).status
        == "applied"
    )
    before, revision = manager.get_snapshot(), manager.revision
    result = apply(
        manager,
        {"operation": "update_node", "node": metric("m", "missing")},
        reviewed_reconciliation=True,
    )
    assert result.status == "rejected"
    assert manager.get_snapshot() == before and manager.revision == revision


def test_rejection_is_atomic_and_feedback_points_to_request(manager):
    before, revision = manager.get_snapshot(), manager.revision
    result = apply(
        manager, create(metric("good", "field")), create(metric("bad", "missing"))
    )
    assert result.status == "rejected"
    error = result.errors[0]
    assert error.code == "REFERENCE_NOT_FOUND"
    assert error.operation_index == 1
    assert error.details["reference"] == "missing"
    assert error.path and error.hint
    assert manager.get_snapshot() == before
    assert manager.revision == revision


def test_update_replaces_stale_dependencies(manager):
    assert (
        apply(manager, create(metric("a", "field")), create(metric("b", "a"))).status
        == "applied"
    )
    updated = metric("b", "field")
    result = apply(manager, {"operation": "update_node", "node": updated})
    assert result.status == "applied", result.errors
    assert [e.edge_type for e in manager.get_edges("b")] == ["grounded_on"]


def test_cycles_rejected_with_actual_path(manager):
    before = manager.get_snapshot()
    result = apply(manager, create(metric("a", "b")), create(metric("b", "a")))
    assert result.status == "rejected"
    assert result.errors[0].code == "METRIC_DEPENDENCY_CYCLE"
    assert set(result.errors[0].details["cycle"]) == {"a", "b"}
    assert manager.get_snapshot() == before


def test_delete_requires_explicit_dependency_repair_in_same_batch(manager):
    assert (
        apply(manager, create(metric("a", "field")), create(metric("b", "a"))).status
        == "applied"
    )
    result = apply(manager, {"operation": "delete_node", "node_id": "a"})
    assert result.status == "rejected"
    assert result.errors[0].code == "NODE_HAS_DEPENDENTS"
    assert "b" in result.errors[0].details["dependent_ids"]
    result = apply(
        manager,
        {"operation": "delete_node", "node_id": "a"},
        {"operation": "update_node", "node": metric("b", "field")},
    )
    assert result.status == "applied", result.errors
    assert manager.get_node("a") is None
    assert manager.get_dependencies("b") == ["field"]


def test_deleting_default_edge_restores_it_from_retained_anchor(manager):
    edge = manager.get_edges("field")[0]
    result = apply(manager, {"operation": "delete_edge", "edge_id": edge.id})
    assert result.status == "applied"
    assert manager.get_dependencies("field") == ["table"]
    assert len(manager.get_edges("field")) == 1


def test_revision_retry_and_reused_id(manager):
    base = manager.revision
    operation = create(metric("a", "field"))
    operations = authored_operations(manager, [operation])
    first = manager.apply(operations, expected_revision=base, request_id="same")
    assert first.status == "applied"
    assert (
        apply(manager, create(metric("b", "a")), request_id="later").status == "applied"
    )
    latest = manager.revision
    repeated = manager.apply(operations, expected_revision=base, request_id="same")
    assert repeated.revision == first.revision
    assert manager.revision == latest
    reused = manager.apply(
        [create(metric("c", "field"))], expected_revision=base, request_id="same"
    )
    assert reused.errors[0].code == "REQUEST_ID_REUSED"
    stale = manager.apply([operation], expected_revision=base, request_id="new")
    assert stale.errors[0].code == "REVISION_MISMATCH"


def test_read_and_result_objects_cannot_mutate_graph(manager):
    result = apply(manager, create(metric("a", "field")))
    manager.get_node("a").properties.clear()
    manager.get_snapshot().graph.nodes[0].properties.clear()
    result.state.graph.nodes[0].properties.clear()
    assert manager.get_node("a").properties["expression"]["refs"] == {"x": "field"}


def test_failed_commit_does_not_advance_memory(manager):
    before, revision = manager.get_snapshot(), manager.revision

    def fail(state, expected):
        assert expected == revision
        raise OSError("disk unavailable")

    result = apply(manager, create(metric("a", "field")), commit=fail)
    assert result.status == "error"
    assert result.errors[0].code == "STORAGE_ERROR"
    assert manager.get_snapshot() == before and manager.revision == revision


def test_table_updates_refresh_generated_column_metadata(manager):
    result = apply(
        manager,
        {
            "operation": "update_node",
            "node": node(
                "table",
                "table",
                {"table_name": "orders", "important_columns": ["field"]},
            ),
        },
    )
    assert result.status == "applied", result.errors
    assert manager.get_edges("field")[0].properties["is_important"]


def test_explicit_join_and_referenced_columns_are_validated(manager):
    assert (
        apply(
            manager,
            create(node("other", "table", {"table_name": "other"})),
            create(
                node(
                    "other_field",
                    "column",
                    {"table_ref": "other", "column_name": "x", "data_type": "REAL"},
                )
            ),
        ).status
        == "applied"
    )
    edge = {
        "id": "join",
        "edge_type": "joins_with",
        "from_node_id": "table",
        "to_node_id": "other",
        "properties": {
            "column_pairs": [
                {"from_column_ref": "field", "to_column_ref": "other_field"}
            ]
        },
    }
    assert (
        apply(manager, {"operation": "create_edge", "edge": edge}).status == "applied"
    )
    result = apply(manager, {"operation": "delete_node", "node_id": "field"})
    assert result.errors[0].code == "NODE_HAS_DEPENDENTS"
    assert "join" in result.errors[0].details["dependent_ids"]


def test_wrong_fields_and_privilege_are_not_ignored(manager):
    result = apply(manager, {**create(metric("a", "field")), "force": True})
    assert result.errors[0].code == "INVALID_FIELD"
    result = apply(
        manager,
        create(node("t", "table", {"table_name": "t"})),
        allowed_node_types=frozenset({"concept", "metric", "generation_rule"}),
    )
    assert result.errors[0].code == "OPERATION_NOT_ALLOWED"


def test_reference_remapping_preserves_literals_and_prose():
    properties = {
        "references": ["old"],
        "expression": {"expr": "old", "refs": {"x": "old"}},
        "action": "old",
        "targets": [["old"]],
        "target_expressions": {"0": "old"},
    }
    mapped = remap_references(properties, {"old": "canonical"})
    assert mapped["references"] == ["canonical"]
    assert mapped["expression"]["refs"] == {"x": "canonical"}
    assert (
        mapped["expression"]["expr"]
        == mapped["action"]
        == mapped["target_expressions"]["0"]
        == "old"
    )
    assert properties["references"] == ["old"]


def test_agent_can_correct_rejected_batch_without_losing_work(case_graph):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    store, _, _ = case_graph
    tools = {t.name: t for t in build_tools(session_for(case_graph))}
    base = store.working_revision
    bad = {"id": "concept.new", "node_type": "concept", "name": "New", "properties": {"targets": [["column.missing.nope"]]}}
    rejected = tools["edit_graph"].invoke(
        {"edit_id": "bad", "expected_revision": base, "upserts": [bad]}
    )
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "REFERENCE_NOT_FOUND"
    assert rejected["errors"][0]["details"]["reference"] == "column.missing.nope"
    assert store.working_revision == base
    bad["properties"]["targets"] = [["column.orders.amount"]]
    accepted = tools["edit_graph"].invoke(
        {"edit_id": "corrected", "expected_revision": base, "upserts": [bad]}
    )
    assert accepted["status"] == "ok"
    tools["inspect_graph"].invoke({"mode": "edges", "node_id": "concept.new"})
    assert not store.manager().get_edges("concept.new")
    revision = store.working_revision
    deleted = tools["edit_graph"].invoke(
        {
            "edit_id": "delete",
            "expected_revision": revision,
            "delete_ids": ["concept.new"],
        }
    )
    assert deleted["status"] == "ok"
    assert not store.working.semantic_assets


def test_agent_invalid_nested_fields_return_locations(case_graph):
    from conftest import make_rule
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    store, _, _ = case_graph
    tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    payload = make_rule().model_dump(mode="json")
    payload["properties"]["invented_field"] = True
    base = store.working_revision
    result = tool.invoke(
        {"edit_id": "wrong", "expected_revision": base, "upserts": [payload]}
    )
    assert result["status"] == "rejected"
    assert result["errors"][0]["code"] == "INVALID_FIELD"
    assert "invented_field" in result["errors"][0]["path"]
    assert result["errors"][0]["hint"]
    assert store.working_revision == base


def test_snapshot_reads_saved_graph_without_reprojection(case_graph, monkeypatch):
    from cortex.capabilities.sql_training.graph import snapshots

    store, _, _ = case_graph
    snap = store.working
    assert snap.graph is not None

    def forbidden(*args, **kwargs):
        raise AssertionError("normal reads must not project authoring assets")

    monkeypatch.setattr(snapshots, "build_graph", forbidden)
    assert store.validation_graph(snap.revision) == snap.graph
    assert store.manager().get_snapshot().graph == snap.graph


def test_manager_repository_commit_cas_and_staged_validation(
    manager, tmp_path, monkeypatch
):
    from cortex.application.ontology_graph import GraphRejected
    from cortex.infrastructure.artifacts.graph_state import (
        FileSystemGraphStateRepository,
    )

    repo = FileSystemGraphStateRepository(tmp_path / "graph", load_root_schema())
    state = manager.get_snapshot()
    first = repo.commit(state, expected_revision="empty")
    loaded, revision = repo.load_snapshot()
    assert revision == first and loaded == state
    with pytest.raises(GraphRejected) as caught:
        repo.commit(state, expected_revision="empty")
    assert caught.value.issues[0].code == "REVISION_MISMATCH"
    assert repo.revision == first
    real_load = repo._load_generation

    def reject_staged(generation_id):
        if generation_id != first:
            raise ValueError("staged generation failed")
        return real_load(generation_id)

    monkeypatch.setattr(repo, "_load_generation", reject_staged)
    with pytest.raises(ValueError, match="staged"):
        repo.commit(state, expected_revision=first)
    assert repo.revision == first
    assert repo.load() == state


def test_durable_commit_callback_and_cross_manager_revision(manager, tmp_path):
    from cortex.infrastructure.artifacts.graph_state import (
        FileSystemGraphStateRepository,
    )

    repo = FileSystemGraphStateRepository(tmp_path / "graph", load_root_schema())
    revision = repo.commit(manager.get_snapshot(), expected_revision="empty")
    first = GraphManager(load_root_schema(), repo.load(), revision=revision)
    second = GraphManager(load_root_schema(), repo.load(), revision=revision)
    result = apply(first, create(metric("a", "field")), commit=repo.commit)
    assert result.status == "applied"
    assert first.revision == repo.revision
    unchanged = second.get_snapshot()
    stale = apply(second, create(metric("b", "field")), commit=repo.commit)
    assert stale.status == "rejected"
    assert stale.errors[0].code == "REVISION_MISMATCH"
    assert second.get_snapshot() == unchanged
    assert {n.id for n in repo.load().graph.nodes} == {"table", "field", "a"}


def test_explicit_edge_errors_have_relation_code_and_operation_index(manager):
    edge = {
        "id": "bad",
        "edge_type": " ",
        "from_node_id": "table",
        "to_node_id": "table",
        "properties": {},
    }
    result = apply(manager, {"operation": "create_edge", "edge": edge})
    assert result.status == "rejected"
    assert result.errors[0].code == "INVALID_FIELD"
    assert result.errors[0].operation_index == 0
    assert "bad" in result.errors[0].path


def test_invalid_join_columns_return_actionable_code(manager):
    edge = {
        "id": "bad_join",
        "edge_type": "joins_with",
        "from_node_id": "table",
        "to_node_id": "table",
        "properties": {
            "column_pairs": [{"from_column_ref": "missing", "to_column_ref": "field"}]
        },
    }
    result = apply(manager, {"operation": "create_edge", "edge": edge})
    assert result.status == "rejected"
    assert result.errors[0].code == "INVALID_JOIN_COLUMNS"
    assert result.errors[0].operation_index == 0
    assert result.errors[0].hint


def test_generic_root_types_are_not_writable_nl2sql_nodes(manager):
    before = manager.get_snapshot()
    result = apply(
        manager, create(node("example", "example", {"task": "q", "sql": "SELECT 1"}))
    )
    assert result.status == "rejected"
    assert result.errors[0].code == "UNKNOWN_NODE_TYPE"
    assert result.errors[0].operation_index == 0
    assert manager.get_snapshot() == before


def test_generated_cases_isolate_same_ids_and_local_references(manager):
    from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph
    from cortex.capabilities.sql_training.models import NodeAsset

    assets = [
        NodeAsset.model_validate(metric("metric.base", "column.orders.amount")),
        NodeAsset.model_validate(metric("metric.dependent", "metric.base")),
    ]
    before = [a.model_dump() for a in assets]
    state = build_incoming_graph(
        load_root_schema(),
        assets=[],
        cases=[(key, assets, [
            GraphEdge(id="base_operand", edge_type="grounded_on", from_node_id="metric.base", to_node_id="column.orders.amount", properties={"description": "Amount supplies the base formula."}),
            GraphEdge(id="dependent_operand", edge_type="depends_on", from_node_id="metric.dependent", to_node_id="metric.base", properties={"description": "Base result supplies the dependent formula."}),
        ]) for key in ("first", "second")],
        existing=manager.get_snapshot().graph,
    )
    assert len(state.graph.nodes) == 4
    assert len({n.id for n in state.graph.nodes}) == 4
    bases = {n.id for n in state.graph.nodes if n.name == "metric.base"}
    dependent_refs = [
        next(iter(n.properties["expression"]["refs"].values()))
        for n in state.graph.nodes
        if n.name == "metric.dependent"
    ]
    assert set(dependent_refs) == bases
    assert {
        e.to_node_id for e in state.graph.edges if e.edge_type == "grounded_on"
    } == {"field"}
    assert [a.model_dump() for a in assets] == before


def test_generated_cases_do_not_borrow_other_cases_semantic_ids(manager):
    from conftest import make_rule

    from cortex.application.ontology_graph import GraphRejected
    from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph

    with pytest.raises(GraphRejected) as rejected:
        build_incoming_graph(
            load_root_schema(),
            assets=[],
            cases=[
                ("first", [make_rule("base")], []),
                (
                    "second",
                    [NodeAsset.model_validate(metric("metric.dependent", "metric.base"))],
                    [GraphEdge(id="missing_cross_case", edge_type="depends_on", from_node_id="metric.dependent", to_node_id="metric.base", properties={"description": "Dependency must exist in this case."})],
                ),
            ],
            existing=manager.get_snapshot().graph,
        )
    error = rejected.value.feedback()["errors"][0]
    assert error["code"] == "REFERENCE_NOT_FOUND"
    assert error["details"]["reference"] == "metric.base"
    assert error["path"] and error["hint"]


def test_generated_value_reference_uses_structured_canonical_owner(manager):

    from cortex.capabilities.sql_training.core.ids import make_node_id
    from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph

    assert (
        apply(
            manager,
            create(
                node(
                    "stored-value",
                    "dimension_value",
                    {
                        "column_ref": "field",
                        "value": "a / b",
                    },
                )
            ),
        ).status
        == "applied"
    )
    ref = make_node_id("dimension_value", "orders", "amount", "a / b")
    state = build_incoming_graph(
        load_root_schema(),
        assets=[],
        cases=[("case", [NodeAsset.model_validate({"id": "concept.value", "node_type": "concept", "name": "Stored value", "properties": {"targets": [[ref]]}})], [GraphEdge(id="value_binding", edge_type="grounded_on", from_node_id="concept.value", to_node_id=ref, properties={"description": "Exact observed category value."})])],
        existing=manager.get_snapshot().graph,
    )
    assert state.graph.nodes[0].properties["targets"] == [["stored-value"]]
    assert state.graph.edges[0].to_node_id == "stored-value"


def test_generated_duplicate_case_rejected(manager):
    from conftest import make_rule

    from cortex.application.ontology_graph import GraphRejected
    from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph

    with pytest.raises(GraphRejected):
        build_incoming_graph(
            load_root_schema(),
            assets=[],
            cases=[("same", [make_rule()], []), ("same", [make_rule()], [])],
            existing=manager.get_snapshot().graph,
        )


def test_incoming_catalog_join_remaps_endpoints_and_column_pairs(manager):
    from cortex.capabilities.sql_training.graph.nodes import build_incoming_graph
    from cortex.schema import GraphEdge

    assert (
        apply(
            manager,
            create(node("other-table", "table", {"table_name": "other"})),
            create(
                node(
                    "other-field",
                    "column",
                    {
                        "table_ref": "other-table",
                        "column_name": "amount",
                        "data_type": "REAL",
                    },
                )
            ),
        ).status
        == "applied"
    )
    edge = GraphEdge(
        id="catalog-join",
        edge_type="joins_with",
        from_node_id="table.orders",
        to_node_id="table.other",
        properties={
            "description": "Join order sources by the observed amount field.",
            "column_pairs": [
                {
                    "from_column_ref": "column.orders.amount",
                    "to_column_ref": "column.other.amount",
                }
            ]
        },
    )
    before = edge.model_dump()
    state = build_incoming_graph(
        load_root_schema(),
        assets=[],
        cases=[],
        existing=manager.get_snapshot().graph,
        catalog_edges=[edge],
    )
    assert not state.graph.nodes
    (actual,) = state.graph.edges
    assert (actual.from_node_id, actual.to_node_id) == ("table", "other-table")
    assert actual.properties["column_pairs"] == [
        {
            "from_column_ref": "field",
            "to_column_ref": "other-field",
        }
    ]
    assert edge.model_dump() == before


@pytest.mark.parametrize(
    "operations", [None, 3, "create", {"operation": "create_node"}]
)
def test_malformed_batch_returns_feedback_without_mutation(manager, operations):
    before = manager.get_snapshot()
    result = manager.apply(
        operations, expected_revision=manager.revision, request_id="invalid-batch"
    )
    assert result.status == "rejected"
    assert result.errors[0].code == "INVALID_OPERATION"
    assert manager.get_snapshot() == before


def test_rule_sql_trigger_is_checked_without_becoming_a_dependency_edge(manager):
    from conftest import make_rule

    from cortex.capabilities.sql_training.graph.nodes import build_node

    rule = build_node(make_rule()).model_dump(mode="json")
    rule["properties"]["triggers"]["sql"]["columns"] = ["missing"]
    before = manager.get_snapshot()
    bad = apply(manager, create(rule))
    assert bad.status == "rejected" and bad.errors[0].code == "REFERENCE_NOT_FOUND"
    assert bad.errors[0].operation_index == 0
    assert manager.get_snapshot() == before
    rule["properties"]["triggers"]["sql"]["columns"] = ["table"]
    assert apply(manager, create(rule)).errors[0].code == "INVALID_ENDPOINT_TYPE"
    rule["properties"]["triggers"]["sql"]["columns"] = ["field"]
    assert apply(manager, create(rule)).status == "applied"
    assert manager.get_dependencies(rule["id"]) == []
    assert manager.get_edges(rule["id"]) == []
    assert rule["id"] not in manager.get_dependents("field")
    rejected = apply(manager, {"operation": "delete_node", "node_id": "field"})
    assert rejected.errors[0].code == "NODE_HAS_DEPENDENTS"


def test_catalog_metadata_uses_real_column_owners_and_blocks_deletion(manager):
    table = node(
        "table",
        "table",
        {
            "table_name": "orders",
            "primary_keys": ["amount"],
            "unique_keys": [["amount"]],
        },
    )
    assert (
        apply(manager, {"operation": "update_node", "node": table}).status == "applied"
    )
    assert manager.get_dependencies("table") == []
    assert "table" not in manager.get_dependents("field")
    before = manager.get_snapshot()
    assert (
        apply(manager, {"operation": "delete_node", "node_id": "field"}).status
        == "rejected"
    )
    assert manager.get_snapshot() == before
    table["properties"]["primary_keys"] = ["missing"]
    result = apply(manager, {"operation": "update_node", "node": table})
    assert (
        result.status == "rejected" and result.errors[0].code == "REFERENCE_NOT_FOUND"
    )


def test_conflict_content_remaps_nested_slots_without_rewriting_sql():
    content = {
        "name": "old",
        "properties": {
            "references": ["old"],
            "expression": {"expr": "old + ${x}", "refs": {"x": "old"}},
            "targets": [["old"]],
            "target_expressions": {"0": "old"},
            "triggers": {"sql": {"columns": ["old"]}},
        },
    }
    mapped = remap_references(content, {"old": "canonical"})
    assert mapped["properties"]["references"] == ["canonical"]
    assert mapped["properties"]["expression"]["refs"] == {"x": "canonical"}
    assert mapped["properties"]["targets"][0] == ["canonical"]
    assert mapped["properties"]["triggers"]["sql"]["columns"] == ["canonical"]
    assert (
        mapped["name"] == "old"
        and mapped["properties"]["target_expressions"]["0"] == "old"
    )
    assert content["properties"]["references"] == ["old"]


@pytest.mark.parametrize("mode", ["dangling", "formula", "missing_edge", "cycle"])
def test_direct_repository_commit_cannot_bypass_graph_constraints(
    manager, tmp_path, mode
):
    from cortex.application.ontology_graph import GraphRejected
    from cortex.infrastructure.artifacts.graph_state import (
        FileSystemGraphStateRepository,
    )
    from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState

    repo = FileSystemGraphStateRepository(tmp_path / "graph", load_root_schema())
    revision = repo.commit(manager.get_snapshot(), expected_revision="empty")
    bad = metric("m", "missing" if mode == "dangling" else "field")
    if mode == "formula":
        bad["properties"]["expression"]["refs"] = {}
    nodes = (*manager.get_snapshot().graph.nodes, GraphNode.model_validate(bad))
    edges = manager.get_snapshot().graph.edges
    if mode == "cycle":
        nodes = (
            *manager.get_snapshot().graph.nodes,
            GraphNode.model_validate(metric("m", "m")),
        )
        edges = (
            *edges,
            GraphEdge(
                id="self", edge_type="depends_on", from_node_id="m", to_node_id="m", properties={"description": "Self-referential calculation."}
            ),
        )
    with pytest.raises(GraphRejected) as caught:
        repo.commit(
            GraphState(graph=GraphSpace(nodes=nodes, edges=edges), node_conflicts=()),
            expected_revision=revision,
        )
    assert (
        caught.value.issues[0].code
        == {
            "dangling": "REFERENCE_NOT_FOUND",
            "formula": "INVALID_FIELD",
            "missing_edge": "MISSING_FORMULA_RELATION",
            "cycle": "METRIC_DEPENDENCY_CYCLE",
        }[mode]
    )
    assert repo.revision == revision
    assert repo.load() == manager.get_snapshot()


def test_retyping_edge_owner_does_not_bypass_physical_permissions(manager):
    assert (
        apply(manager, create(node("c", "concept", {"definition": "x", "targets": [["field"]]}))).status
        == "applied"
    )
    assert (
        apply(
            manager,
            {
                "operation": "create_edge",
                "edge": {
                    "id": "owned-by-table",
                    "edge_type": "grounded_on",
                    "from_node_id": "c",
                    "to_node_id": "field",
                    "properties": {},
                },
            },
        ).status
        == "applied"
    )
    # A custom role allowed only Metrics must not take over an existing Concept edge.
    assert apply(manager, create(metric("m", "field"))).status == "applied"
    before = manager.get_snapshot()
    result = apply(
        manager,
        {
            "operation": "update_edge",
            "edge": {
                "id": "owned-by-table",
                "edge_type": "grounded_on",
                "from_node_id": "m",
                "to_node_id": "field",
                "properties": {},
            },
        },
        allowed_node_types=frozenset({"metric"}),
    )
    assert (
        result.status == "rejected" and result.errors[0].code == "OPERATION_NOT_ALLOWED"
    )
    assert manager.get_snapshot() == before


def test_generation_rejects_invalid_formula_before_merge_work(manager):
    from cortex.application.ontology_graph import GraphRejected
    from cortex.bootstrap.graph_merge import build_graph_merge_operator
    from cortex.config import CortexConfig
    from cortex.schema import GraphNode, GraphSpace, GraphState

    bad = metric("m", "field")
    bad["properties"]["expression"]["refs"] = {}
    operator = build_graph_merge_operator(CortexConfig(), live_semantics=False)
    incoming = GraphState(
        graph=GraphSpace(nodes=(GraphNode.model_validate(bad),)), node_conflicts=()
    )
    with pytest.raises(GraphRejected) as caught:
        operator.merge(manager.get_snapshot(), incoming, load_root_schema())
    assert caught.value.issues[0].code == "INVALID_FIELD"


def test_duplicate_saved_relations_are_rejected_instead_of_silently_repaired(manager):
    from cortex.application.ontology_graph import GraphRejected
    from cortex.schema import GraphSpace
    state = manager.get_snapshot()
    original = state.graph.edges[0]
    duplicate = original.model_copy(update={"id": "duplicate"})
    invalid = state.model_copy(update={"graph": GraphSpace(nodes=state.graph.nodes, edges=(original, duplicate))})
    with pytest.raises(GraphRejected, match="same relation"):
        manager.prepare_state(invalid)
    with pytest.raises(GraphRejected, match="same relation"):
        manager.validate_state(invalid)


def test_subclass_cycle_is_left_to_semantic_equivalence_assessment(manager):
    assert (
        apply(
            manager,
            create(node("a", "concept", {"definition": "A", "targets": [["field"]]})),
            create(node("b", "concept", {"definition": "B", "targets": [["field"]]})),
        ).status
        == "applied"
    )
    result = apply(
        manager,
        *[
            {
                "operation": "create_edge",
                "edge": {
                    "id": edge_id,
                    "edge_type": "is_a",
                    "from_node_id": source,
                    "to_node_id": target,
                },
            }
            for edge_id, source, target in [("ab", "a", "b"), ("ba", "b", "a")]
        ],
    )
    assert result.status == "applied", result.errors
    assert {e.id for e in manager.get_edges(edge_type="is_a")} == {"ab", "ba"}


def test_concept_targets_reject_removed_fields(
    manager,
):
    bad = node(
        "c",
        "concept",
        {
            "definition": "x",
            "targets": [
                {
                    "id": "t",
                    "sql_expression": "amount",
                    "references": ["field"],
                    "binding_operator": "one_of",
                }
            ],
        },
    )
    assert apply(manager, create(bad)).errors[0].code == "INVALID_FIELD"
    bad["properties"]["targets"][0] = {
        "id": "t",
        "sql_expression": "amount",
        "endpoint_refs": ["missing"],
        "support_refs": [],
    }
    result = apply(manager, create(bad))
    assert result.status == "rejected" and result.errors[0].code == "INVALID_FIELD"


def test_incomplete_rule_write_is_rejected(manager):
    result = apply(manager, create(node("rule", "generation_rule", {})))
    assert result.status == "rejected" and result.errors[0].code == "INVALID_FIELD"


def test_self_join_is_not_accepted_as_an_ordinary_physical_join(manager):
    result = apply(
        manager,
        {
            "operation": "create_edge",
            "edge": {
                "id": "self",
                "edge_type": "joins_with",
                "from_node_id": "table",
                "to_node_id": "table",
                "properties": {
                    "column_pairs": [
                        {"from_column_ref": "field", "to_column_ref": "field"}
                    ]
                },
            },
        },
    )
    assert (
        result.status == "rejected" and result.errors[0].code == "INVALID_JOIN_COLUMNS"
    )


def conflicted_metric_state(manager):
    from cortex.schema import (
        GraphNode,
        GraphSpace,
        GraphState,
        MergeStatus,
    )

    owner = GraphNode.model_validate(metric("m", "field")).model_copy(
        update={"merge_status": MergeStatus.CONFLICT}
    )
    content = owner.model_dump(
        include={"name", "description", "aliases", "tags", "properties"}
    )
    from cortex.schema import NodeContent

    owner, conflict = manager.compact_conflict(
        owner,
        [
            ("a", NodeContent.model_validate(content), ()),
            (
                "b",
                NodeContent.model_validate(
                    {**content, "description": "Another interpretation"}
                ),
                (),
            ),
        ],
    )
    raw = GraphState(
        graph=GraphSpace(
            nodes=(*manager.get_snapshot().graph.nodes, owner),
            edges=(*manager.get_snapshot().graph.edges,
                   GraphEdge(
                       id="explicit_m_field", edge_type="grounded_on", from_node_id="m", to_node_id="field",
                       properties={"description": "Both branches use the stored field."})),
        ),
        node_conflicts=(conflict,),
    )
    return manager.prepare_state(raw)[0]


def test_conflicted_owner_cannot_be_overwritten_through_crud(manager):
    state = conflicted_metric_state(manager)
    conflicted = GraphManager(load_root_schema(), state)
    replacement = state.graph.nodes[-1].model_dump(mode="json")
    replacement["description"] = "silently replace alternatives"
    result = apply(conflicted, {"operation": "update_node", "node": replacement})
    assert (
        result.status == "rejected"
        and result.errors[0].code == "CONFLICT_REQUIRES_RECONCILIATION"
    )
    assert conflicted.get_snapshot() == state


def test_each_conflict_branch_checks_its_own_metric_cycles(manager):
    from cortex.application.ontology_graph import GraphRejected

    state = conflicted_metric_state(manager)
    props = metric("m", "m")["properties"]
    # Both branches cover the same paths; only A introduces a cycle.
    state.conflict_nodes[0].alternatives[0].overrides.update(
        {f"properties.{k}": v for k, v in props.items()}
    )
    state.conflict_nodes[0].alternatives[1].overrides.update(
        {f"properties.{k}": v for k, v in metric("m", "field")["properties"].items()}
    )
    owner = next(n for n in state.graph.nodes if n.id == "m")
    owner.properties.clear()
    # The physical operand is used only by branch B after this edit.
    state = state.model_copy(update={"graph": state.graph.model_copy(update={"edges": tuple(
        e.model_copy(update={"properties": {**e.properties, "alternative_ids": ["b"]}})
        if e.id == "explicit_m_field" else e for e in state.graph.edges)})})
    state = state.model_copy(update={"graph": state.graph.model_copy(update={"edges": (*state.graph.edges, GraphEdge(id="branch_self", edge_type="depends_on", from_node_id="m", to_node_id="m", properties={"description": "Invalid recursive branch operand.", "alternative_ids": ["a"]}))})})
    with pytest.raises(GraphRejected) as caught:
        manager.prepare_state(state)
    assert caught.value.issues[0].code == "METRIC_DEPENDENCY_CYCLE"
    assert caught.value.issues[0].details["alternative_id"] == "a"


@pytest.mark.parametrize(
    "kind, properties",
    [
        ("column", {"column_name": "x", "data_type": "REAL"}),
        ("dimension_value", {"value": "x"}),
    ],
)
def test_physical_node_requires_explicit_owner(manager, kind, properties):
    result = apply(manager, create(node("orphan", kind, properties)))
    assert result.status == "rejected" and result.errors[0].code == "INVALID_FIELD"


def test_exact_physical_identity_is_immutable_under_update(manager):
    before = manager.get_snapshot()
    result = apply(
        manager,
        {
            "operation": "update_node",
            "node": node(
                "field",
                "column",
                {
                    "table_ref": "table",
                    "column_name": "different",
                    "data_type": "REAL",
                },
            ),
        },
    )
    assert (
        result.status == "rejected"
        and result.errors[0].code == "IMMUTABLE_NODE_IDENTITY"
    )
    assert manager.get_snapshot() == before


def test_cached_snapshot_cannot_hide_invalid_content_behind_fingerprints(case_graph):
    import json

    from cortex.application.ontology_graph import GraphRejected
    from cortex.capabilities.sql_training.graph.snapshots import fingerprint

    store, _, _ = case_graph
    snapshot = store.working
    payload = snapshot.model_dump(mode="json")
    from cortex.schema import GraphNode

    payload["graph"]["nodes"].append(
        GraphNode.model_validate(metric("m", "missing")).model_dump(mode="json")
    )
    payload["graph_fingerprint"] = fingerprint(payload["graph"])
    from cortex.schema import GraphSpace

    payload["content_fingerprint"] = store._content_hash(
        snapshot.assets,
        snapshot.catalog_edges,
        snapshot.explicit_edges,
        GraphSpace.model_validate(payload["graph"]),
    )
    (store.root / f"{snapshot.revision}.json").write_text(json.dumps(payload))
    with pytest.raises(GraphRejected) as caught:
        store.validation_graph(snapshot.revision)
    assert caught.value.issues[0].code == "REFERENCE_NOT_FOUND"


def test_completed_merge_checkpoint_cannot_bypass_final_validation(
    manager, monkeypatch
):
    from cortex.application.merge_graph import GraphMergeResult
    from cortex.application.ontology_graph import GraphRejected
    from cortex.bootstrap.graph_merge import build_graph_merge_operator
    from cortex.capabilities.graph_merge.workflow import run
    from cortex.config import CortexConfig
    from cortex.schema import GraphNode, GraphSpace, GraphState

    incoming = GraphState(
        graph=GraphSpace(nodes=(GraphNode.model_validate(metric("m", "field")),), edges=(GraphEdge(id="incoming_operand", edge_type="grounded_on", from_node_id="m", to_node_id="field", properties={"description": "Input from existing catalog."}),)),
        node_conflicts=(),
    )
    cached = GraphState(
        graph=GraphSpace(nodes=(GraphNode.model_validate(metric("bad", "missing")),)),
        node_conflicts=(),
    )
    monkeypatch.setattr(
        run,
        "run_merge",
        lambda *args, **kwargs: GraphMergeResult(
            state=cached, node_mappings=(), edge_mappings=(), diagnostics=()
        ),
    )
    operator = build_graph_merge_operator(CortexConfig(), live_semantics=False)
    with pytest.raises(GraphRejected) as caught:
        operator.merge(manager.get_snapshot(), incoming, load_root_schema())
    assert caught.value.issues[0].code == "REFERENCE_NOT_FOUND"


def test_canonical_graph_rejects_duplicate_physical_business_keys(manager):
    result = apply(
        manager,
        create(
            node(
                "alias",
                "column",
                {"column_name": "amount", "table_ref": "table", "data_type": "REAL"},
            )
        ),
    )
    assert (
        result.status == "rejected"
        and result.errors[0].code == "DUPLICATE_NODE_IDENTITY"
    )
    assert result.errors[0].details["existing_node_id"] == "field"


def test_case_fragments_require_real_endpoints_and_detect_metric_cycles(manager):
    from cortex.application.ontology_graph import GraphRejected
    from cortex.capabilities.sql_training.graph.nodes import build_node
    from cortex.schema import GraphSpace, GraphState
    cyclic = [NodeAsset.model_validate(metric("metric.a", "metric.b")), NodeAsset.model_validate(metric("metric.b", "metric.a"))]
    relations = tuple(GraphEdge(id=f"operand_{i}", edge_type="depends_on", from_node_id=owner, to_node_id=operand, properties={"description": "Invalid recursive calculation."}) for i, (owner, operand) in enumerate((("metric.a", "metric.b"), ("metric.b", "metric.a"))))
    with pytest.raises(GraphRejected) as caught:
        manager.prepare_fragment(GraphState(graph=GraphSpace(nodes=tuple(build_node(a) for a in cyclic), edges=relations)), existing=manager.get_snapshot().graph)
    assert caught.value.issues[0].code == "METRIC_DEPENDENCY_CYCLE"
    missing = NodeAsset.model_validate(metric("metric.a", "column.missing.amount"))
    with pytest.raises(GraphRejected) as caught:
        manager.prepare_fragment(GraphState(graph=GraphSpace(nodes=(build_node(missing),), edges=(GraphEdge(id="missing", edge_type="grounded_on", from_node_id=missing.id, to_node_id="column.missing.amount", properties={"description": "An unresolved input."}),))), existing=manager.get_snapshot().graph)
    assert caught.value.issues[0].code == "REFERENCE_NOT_FOUND"


def test_agent_injected_relation_fields_follow_the_active_schema():
    from cortex.capabilities.sql_training.learning.inputs import ontology_contract
    from cortex.schema.relationships import MANAGED_RELATIONS

    schema = load_root_schema()
    contract = ontology_contract(schema)
    for relation in contract["relations"]:
        spec = schema.edge_type_index()[relation["type"]]
        assert relation["properties"] == {
            name: field.model_dump(mode="json", exclude_none=True)
            for name, field in spec.fields.items()
        }
        assert relation["managed_from"] == [
            kind
            for kind in relation["from"]
            if relation["type"] in MANAGED_RELATIONS.get(kind, ())
        ]
        assert relation["directionality"] == spec.directionality.value
