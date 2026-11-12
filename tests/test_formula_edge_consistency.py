"""Formula edits must preserve exactly their direct operand relations."""

from test_graph_manager import apply, create, metric, node

from cortex.application.ontology_graph import GraphManager
from cortex.schema import load_root_schema


def seeded_manager():
    manager = GraphManager(load_root_schema())
    result = apply(
        manager,
        create(node("table", "table", {"table_name": "items"})),
        create(node("x", "column", {"table_ref": "table", "column_name": "x"})),
        create(node("y", "column", {"table_ref": "table", "column_name": "y"})),
        create(node("a", "concept", {"targets": [["x"]]})),
        create(node("b", "concept", {"targets": [["y"]]})),
        create(node("m", "metric", {"expression": {
            "expr": "${a} / ${b}", "refs": {"a": "a", "b": "b"}}})),
    )
    assert result.status == "applied", result.errors
    return manager


def test_formula_edit_rejects_stale_direct_operand_and_is_atomic():
    manager = seeded_manager()
    before = manager.get_snapshot()
    # Bypass the test helper that supplies the author's formula edge edits.
    update = {"operation": "update_node", "node": metric("m", "a")}
    result = manager.apply([update], expected_revision=manager.revision, request_id="stale")
    assert result.status == "rejected"
    assert result.errors[0].code == "STALE_FORMULA_RELATION"
    assert manager.get_snapshot() == before
    stale = next(e for e in manager.get_edges("m") if e.to_node_id == "b")
    result = manager.apply(
        [update, {"operation": "delete_edge", "edge_id": stale.id}],
        expected_revision=manager.revision, request_id="complete",
    )
    assert result.status == "applied", result.errors


def test_repeated_operand_uses_one_relation_and_indirect_dependency_is_not_a_direct_operand():
    manager = seeded_manager()
    result = apply(manager, {"operation": "update_node", "node": node("m", "metric", {
        "expression": {"expr": "${a} / (${a} + ${b})", "refs": {"a": "a", "b": "b"}}
    })}, create(metric("outer", "m")))
    assert result.status == "applied", result.errors
    assert len(manager.get_edges("m", edge_type="depends_on")) == 2
    assert {e.to_node_id for e in manager.get_edges("outer", edge_type="depends_on")} == {"m"}
    result = manager.apply([{"operation": "create_edge", "edge": {
        "id": "incorrect_shortcut", "edge_type": "depends_on", "from_node_id": "outer",
        "to_node_id": "a", "properties": {"description": "Indirect input incorrectly stored as direct."}
    }}], expected_revision=manager.revision, request_id="shortcut")
    assert result.status == "rejected"
    assert result.errors[0].code == "STALE_FORMULA_RELATION"
