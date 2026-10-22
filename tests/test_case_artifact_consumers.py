import json

import pytest
from conftest import make_rule

from ablation_study.extraction.bundles import finalize_collection, write_case
from cortex.capabilities.sql_training.artifacts.cases import load_case_assets
from cortex.capabilities.sql_training.merge_evaluation import (
    _load_cases,
    _load_evidence,
)
from cortex.infrastructure.online_context.query_examples import load_query_example_index


def test_ablation_export_is_consumed_without_duplicate_case_files(case_graph, tmp_path):
    _, _, physical = case_graph
    root = tmp_path / "export"
    record = {"source_id": "case-1", "question": "How many?", "sql": "SELECT 1"}
    asset = make_rule(references=["column.orders.id"])
    entry = write_case(
        root,
        record,
        [asset],
        arm="direct",
        result={"correct": None, "asset_outcome": "unassessed"},
        provenance={"snapshot": "frozen"},
        case_index=1,
    )
    finalize_collection(
        root,
        spec={
            "records": [record],
            "physical_workspace": str(physical.root),
            "dataset": "test",
            "database": "sqlite",
        },
        arm="direct",
        cases=[entry],
    )
    case = root / entry["case_dir"]
    assert {str(p.relative_to(case)) for p in case.rglob("*") if p.is_file()} == {
        "case.json",
        "result/result.json",
        "result/ontology.json",
    }
    assert {p.name for p in root.iterdir()} == {
        "manifest.json",
        "catalog_edges.json",
        "cases",
    }
    result = json.loads((case / "result/result.json").read_text())
    assert "draft_nodes" not in result
    assert load_case_assets(result) == [asset]
    loaded = _load_cases(root)
    assert loaded[0][0] == record["source_id"]
    graph = loaded[0][1]
    assert [n.id for n in graph.nodes] == [asset.id]
    assert graph.edges
    assert all(
        source.case_id == record["source_id"]
        for n in graph.nodes
        for source in n.sources
    )
    assert _load_evidence(root) == {record["source_id"]: record}
    assert load_query_example_index(root)[record["source_id"]].sql == record["sql"]
    assert (
        load_query_example_index(case)[record["source_id"]].question
        == record["question"]
    )

    rows = json.loads((case / "result/ontology.json").read_text())
    rows[0]["name"] = "tampered"
    (case / "result/ontology.json").write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="fingerprint"):
        _load_cases(root)


def test_query_example_index_requires_current_case_source_layout(tmp_path):
    (tmp_path / "source.json").write_text(json.dumps({"source_id": "old"}))
    with pytest.raises(ValueError, match="no case.json"):
        load_query_example_index(tmp_path)


def test_direct_export_consumes_versioned_graph_snapshot(case_graph, tmp_path):
    from ablation_study.extraction.direct import export_direct
    from ablation_study.extraction.io import sha256

    store, _, physical = case_graph
    asset = make_rule(references=["column.orders.id"])
    snapshot = store.publish_edit([*store.working.assets, asset])
    snapshot_path = store.root / f"{snapshot.revision}.json"
    record = {"source_id": "direct", "question": "How many?", "sql": "SELECT 1"}
    root = tmp_path / "direct-export"
    manifest = export_direct(
        {
            "records": [record],
            "physical_workspace": str(physical.root),
            "dataset": "test",
            "database": "sqlite",
            "sources": {
                "direct": {
                    "snapshot": str(snapshot_path),
                    "sha256": sha256(snapshot_path),
                }
            },
        },
        root,
    )
    assert manifest["case_count"] == 1
    assert [n.id for n in _load_cases(root)[0][1].nodes] == [asset.id]
