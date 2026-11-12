"""Inline conflict storage retains integrity checks and imports old bundles once."""

import hashlib
import json
import re

import pytest

from cortex.application.ontology_graph import GraphManager
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_state import (
    FileSystemGraphStateRepository,
    GraphStateBundleError,
)
from cortex.schema import (
    GraphNode,
    GraphSpace,
    GraphState,
    NodeContent,
    SourceReference,
    calculate_schema_hash,
    load_root_schema,
)


@pytest.fixture
def state():
    schema = load_root_schema()
    manager = GraphManager(schema)
    owner = GraphNode(id="rule.count", node_type="generation_rule", name="Count")
    candidates = [
        (
            key,
            NodeContent(
                name="Count",
                description="Counting rule",
                aliases=(),
                tags=(),
                properties={
                    "condition": "The question requests a count.",
                    "action": action,
                    "reason": "Preserve the requested counting grain.",
                    "triggers": {"question": {"phrases": ["how many"]}},
                },
            ),
            (SourceReference(source_id=key, locator="qa", case_id=key),),
        )
        for key, action in (
            ("case.rows", "Use COUNT(*)."),
            ("case.distinct", "Count distinct identifiers."),
        )
    ]
    owner, _ = manager.compact_conflict(
        owner, candidates, preservation_reasons=("Counting grain requires a decision.",)
    )
    return manager.prepare_state(
        GraphState(graph=GraphSpace(nodes=(owner,))), strict=True
    )[0]


def current_generation(repository):
    return repository.root / "generations" / repository.revision


def write_rows(path, rows):
    content = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows).encode()
    path.write_bytes(content)
    return {
        "path": path.name,
        "count": len(rows),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def test_new_generation_contains_conflicts_only_inside_nodes(tmp_path, state):
    repository = FileSystemGraphStateRepository(tmp_path, load_root_schema())
    repository.save(state)
    generation = current_generation(repository)
    manifest = json.loads((generation / "manifest.json").read_text())
    assert manifest["format_version"] == "graph-state-v3-inline"
    assert "conflicts" not in manifest
    assert not (generation / "conflicts.jsonl").exists()
    assert {path.name for path in generation.iterdir()} == {
        "nodes.jsonl",
        "edges.jsonl",
        "manifest.json",
    }
    node = json.loads((generation / "nodes.jsonl").read_text())
    assert node["id"] == state.conflict_nodes[0].id
    assert {
        "consolidation_status",
        "preservation_reasons",
        "alternatives",
        "conflict_points",
    } <= node.keys()
    assert all(alternative["sources"] for alternative in node["alternatives"])
    assert "node_id" not in node
    assert repository.load() == state


def test_v2_import_attaches_metadata_then_next_save_writes_v3(tmp_path, state):
    schema = load_root_schema()
    generation = tmp_path / "generations" / "legacy"
    generation.mkdir(parents=True)
    graph = state.graph.model_dump(mode="json")
    for node in graph["nodes"]:
        for field in (
            "consolidation_status",
            "preservation_reasons",
            "alternatives",
            "conflict_points",
        ):
            node.pop(field, None)
    manifest = {
        "format_version": "graph-state-v2",
        "schema_hash": calculate_schema_hash(schema),
        "nodes": write_rows(generation / "nodes.jsonl", graph["nodes"]),
        "edges": write_rows(generation / "edges.jsonl", graph["edges"]),
        "conflicts": write_rows(
            generation / "conflicts.jsonl",
            [
                node.as_conflict().model_dump(mode="json")
                for node in state.conflict_nodes
            ],
        ),
    }
    (generation / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "CURRENT").write_text("legacy\n")
    repository = FileSystemGraphStateRepository(tmp_path, schema)
    imported = repository.load()
    assert imported == state
    assert "node_conflicts" not in imported.model_dump()
    repository.save(imported)
    new_generation = current_generation(repository)
    assert new_generation != generation
    assert not (new_generation / "conflicts.jsonl").exists()
    new_manifest = json.loads((new_generation / "manifest.json").read_text())
    assert new_manifest["format_version"] == "graph-state-v3-inline"
    assert "conflicts" not in new_manifest and repository.load() == state
    assert (
        generation / "conflicts.jsonl"
    ).exists()  # Historical generation remains immutable.


@pytest.mark.parametrize(
    "corruption,match",
    [("hash", "hash mismatch"), ("count", "count mismatch"), ("schema", "schema hash")],
)
def test_inline_storage_integrity_checks(tmp_path, state, corruption, match):
    repository = FileSystemGraphStateRepository(tmp_path, load_root_schema())
    repository.save(state)
    generation = current_generation(repository)
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if corruption == "hash":
        with (generation / "nodes.jsonl").open("ab") as handle:
            handle.write(b"\n")
    elif corruption == "count":
        manifest["nodes"]["count"] += 1
        manifest_path.write_text(json.dumps(manifest))
    else:
        manifest["schema_hash"] = "sha256:changed"
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(GraphStateBundleError, match=match):
        repository.load()


def test_inline_manifest_rejects_reintroduced_sidecar(tmp_path, state):
    repository = FileSystemGraphStateRepository(tmp_path, load_root_schema())
    repository.save(state)
    generation = current_generation(repository)
    path = generation / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["conflicts"] = {
        "path": "conflicts.jsonl",
        "count": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    path.write_text(json.dumps(manifest))
    with pytest.raises(
        GraphStateBundleError, match="cannot contain a conflict sidecar"
    ):
        repository.load()


def test_html_embeds_same_inline_nodes_and_state_has_no_sidecar(state):
    content = render_graph_html(
        state.graph, load_root_schema(), title="Inline conflicts"
    )
    match = re.search(r"const DATA=(.*?);\n", content)
    assert match is not None
    data = json.loads(match.group(1))
    assert "node_conflicts" not in data and "conflicts" not in data["graph"]
    assert data["meta"]["conflicts"] == 1
    assert data["graph"] == state.graph.model_dump(mode="json")
    assert data["graph"]["nodes"][0]["alternatives"]
    assert "node_conflicts" not in state.model_dump_json()
    assert "node_conflicts" not in GraphState.model_json_schema()["properties"]
