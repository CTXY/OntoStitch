"""Accepted graph, source trace and task progress move as one checkpoint."""

import json
import sqlite3

import pytest

from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.runtime.workspace import (
    WorkspaceCorruption,
    WorkspaceRevisionMismatch,
    WorkspaceStore,
)
from cortex.schema import GraphNode, GraphSpace, GraphState


def state(name="initial"):
    return GraphState(
        graph=GraphSpace(nodes=(GraphNode(id="a", node_type="concept", name=name),))
    )


def payload(name="initial"):
    return {
        "current": state(name),
        "lineage": {"version": 1, "groups": []},
        "history": [],
        "open_tasks": [{"id": "task", "base_revision": 0}],
        "request_fingerprint": "unchanged-source-input",
    }


def test_full_workspace_round_trip_and_detached_values(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    original = payload()
    assert store.load("run") is None
    assert store.save("run", original) == 1
    assert "revision" not in original
    restored = WorkspaceStore(store.path).load("run")
    assert restored["revision"] == 1
    assert GraphState.model_validate(restored["current"]) == original["current"]
    assert restored["lineage"] == original["lineage"]
    restored["open_tasks"].clear()
    assert store.load("run")["open_tasks"]


def test_stale_writer_cannot_overwrite_accepted_graph_or_progress(tmp_path):
    a = WorkspaceStore(tmp_path / "workspace.sqlite")
    b = WorkspaceStore(a.path)
    a.save("run", payload())
    first = b.load("run")
    accepted = payload("accepted")
    accepted["history"] = [{"task": "task", "accepted": True}]
    accepted["open_tasks"] = []
    assert a.save("run", accepted, expected_revision=1) == 2
    with pytest.raises(WorkspaceRevisionMismatch) as error:
        b.save("run", first, expected_revision=1)
    assert error.value.actual == 2
    current = a.load("run")
    assert current["history"] == accepted["history"] and current["open_tasks"] == []
    assert current["current"]["graph"]["nodes"][0]["name"] == "accepted"
    with pytest.raises(WorkspaceRevisionMismatch):
        a.save("run", payload())


def test_invalid_serialization_or_graph_cannot_damage_previous_checkpoint(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.save("run", payload())
    old = store.load("run")
    bad = payload("invalid")
    bad["history"] = [object()]
    with pytest.raises(TypeError):
        store.save("run", bad, expected_revision=1)
    bad = payload()
    bad["current"] = {"graph": {"nodes": [{"id": "a"}]}}
    with pytest.raises(ValueError):
        store.save("run", bad, expected_revision=1)
    assert store.load("run") == old


def test_sql_write_failure_rolls_back_the_whole_row(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.save("run", payload())
    old = store.load("run")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_workspace_update BEFORE UPDATE ON merge_workspaces BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.save("run", payload("must not leak"), expected_revision=1)
    assert store.load("run") == old


def test_initial_revision_zero_and_multiple_runs_are_independent(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    assert store.save("one", payload(), expected_revision=0) == 1
    assert store.save("two", payload("other")) == 1
    assert store.save("one", payload("next"), expected_revision=1) == 2
    assert store.load("two")["revision"] == 1


def test_load_detects_corrupt_payload_and_revision(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.save("run", payload())
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE merge_workspaces SET payload='not json' WHERE run_id='run'")
    with pytest.raises(WorkspaceCorruption):
        store.load("run")
    with sqlite3.connect(store.path) as db:
        broken = payload()
        broken["current"] = broken["current"].model_dump(mode="json")
        broken["revision"] = 99
        db.execute(
            "UPDATE merge_workspaces SET payload=? WHERE run_id='run'",
            (json.dumps(broken),),
        )
    with pytest.raises(WorkspaceCorruption, match="revisions disagree"):
        store.load("run")


def test_store_allows_known_pending_global_grounding_conflict(tmp_path):
    from graph_merge_fixtures import catalog, concept, edge

    unfinished = GraphState(
        graph=GraphSpace(
            nodes=(*catalog(), concept("A", "a"), concept("B", "a")),
            edges=(edge("ea", "A", "a"), edge("eb", "B", "a")),
        )
    )
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    value = payload()
    value["current"] = unfinished
    value["open_tasks"] = [{"kind": "binding_contest", "nodes": ["A", "B"]}]
    store.save("run", value)
    assert GraphState.model_validate(store.load("run")["current"]) == unfinished


def test_persisted_lineage_restores_with_actual_current_and_origins(tmp_path):
    from types import SimpleNamespace

    from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element

    initial = state()
    ctx = SimpleNamespace(
        raw_nodes={"a": initial.graph.nodes[0]}, raw_edges={}, physical_map={}
    )
    index = LineageIndex(ctx)
    current = GraphState(
        graph=GraphSpace(nodes=(GraphNode(id="new", node_type="concept", name="new"),))
    )
    index.observe(
        initial,
        current,
        [
            Change(
                before=(Element(kind="node", id="a"),),
                after=(Element(kind="node", id="new"),),
                explanation="Supported restructuring.",
            )
        ],
    )
    data = payload()
    data.update(
        current=current,
        lineage=index.snapshot(),
        history=[{"accepted": "task"}],
        open_tasks=[],
    )
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.save("run", data)
    restored = store.load("run")
    recovered = LineageIndex.restore(ctx, restored["lineage"])
    assert recovered.source_changes(
        GraphState.model_validate(restored["current"])
    ) == index.source_changes(current)
