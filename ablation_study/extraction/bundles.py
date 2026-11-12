from __future__ import annotations

from pathlib import Path

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.workspace import (
    deserialize_asset_envelope,
    serialize_asset_envelope,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.case import project_case_graph
from cortex.capabilities.sql_training.graph.fingerprints import ontology_fingerprint
from cortex.capabilities.sql_training.graph.nodes import (
    build_graph,
    build_incoming_graph,
)
from cortex.schema import GraphEdge, load_root_schema, validate_graph_state

from .io import fingerprint, read_json, write_json

SEMANTIC = (NodeType.CONCEPT, NodeType.EVENT, NodeType.METRIC, NodeType.GENERATION_RULE)
PHYSICAL = (NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE)


def validate_assets(rows, physical_workspace: Path, *, explicit_edges=()):
    assets = [deserialize_asset_envelope(row) for row in rows]
    if len({a.id for a in assets}) != len(assets):
        raise ValueError("duplicate semantic asset IDs inside one Case")
    if any(a.node_type not in SEMANTIC for a in assets):
        raise ValueError("Case contains non-semantic assets")
    physical = [
        a
        for kind in PHYSICAL
        for a in Workspace(physical_workspace).read_draft_assets(kind)
    ]
    graph = build_graph(
        physical, Workspace(physical_workspace).read_catalog_edges(), load_root_schema()
    )
    project_case_graph(assets, physical_graph=graph, explicit_edges=explicit_edges)
    return assets


def write_case(
    root: Path,
    record: dict,
    assets,
    *,
    arm: str,
    result: dict,
    provenance: dict,
    case_index: int,
    physical_workspace: Path,
    explicit_edges=(),
):
    sid = record["source_id"]
    if not sid or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for c in sid
    ):
        raise ValueError(f"unsafe Case ID: {sid}")
    case = root / "cases" / sid
    rows = [serialize_asset_envelope(a) for a in assets]
    physical = Workspace(physical_workspace)
    graph = build_graph(
        [a for kind in PHYSICAL for a in physical.read_draft_assets(kind)],
        physical.read_catalog_edges(),
        load_root_schema(),
    )
    authored = tuple(GraphEdge.model_validate(e) for e in (explicit_edges or result.get("draft_edges", ())))
    edges = project_case_graph(assets, physical_graph=graph, explicit_edges=authored).edges
    if "draft_nodes" in result:
        raise ValueError("result metadata must not embed ontology assets")
    result_path = case / "result" / "result.json"
    ontology_path = case / "result" / "ontology.json"
    metadata = {
        **result,
        "source_id": sid,
        "result_path": str(result_path.resolve()),
        "ontology_path": str(ontology_path.resolve()),
        "draft_node_ids": sorted(asset.id for asset in assets),
        "draft_node_count": len(assets),
        "draft_edges": [e.model_dump(mode="json") for e in edges],
        "final_ontology_fingerprint": ontology_fingerprint(assets),
        "provenance": provenance,
    }
    write_json(
        case / "case.json",
        {
            "source": record,
            "run_id": f"ablation-{arm}",
            "case_index": case_index,
        },
    )
    write_json(ontology_path, rows)
    write_json(result_path, metadata)
    return {
        "source_id": sid,
        "arm": arm,
        "case_dir": f"cases/{sid}",
        "asset_count": len(rows),
        "edge_count": len(edges),
        "asset_fingerprint": ontology_fingerprint(assets),
        "included_in_merge": bool(rows),
        "asset_outcome": result.get("asset_outcome"),
        "correct": result.get("correct"),
        "semantic_validation": "not_behavior_assessed"
        if arm == "direct"
        else "runtime_assessment",
    }


def finalize_collection(root: Path, *, spec: dict, arm: str, cases: list[dict]):
    if [c["source_id"] for c in cases] != [r["source_id"] for r in spec["records"]]:
        raise ValueError("collection does not preserve the complete frozen Case order")
    physical = Path(spec["physical_workspace"])
    write_json(
        root / "catalog_edges.json",
        [e.model_dump(mode="json") for e in Workspace(physical).read_catalog_edges()],
    )
    manifest = {
        "schema_version": "extraction_ablation.collection.v2",
        "dataset": spec["dataset"],
        "database": spec["database"],
        "arm": arm,
        "spec_fingerprint": fingerprint(spec),
        "case_count": len(cases),
        "cases": cases,
        "merge_performed": False,
        "asset_count": sum(c["asset_count"] for c in cases),
        "empty_case_count": sum(c["asset_count"] == 0 for c in cases),
        "catalog_edges_path": "catalog_edges.json",
        "physical_workspace": str(physical.resolve()),
        "publication_policy": "constructed_candidates"
        if arm == "direct"
        else "accepted_working_including_partial",
        "validation": "structure_and_reference_closure",
        "cases_fingerprint": fingerprint(cases),
    }
    fragments = []
    for case in cases:
        case_result = read_json(root / case["case_dir"] / "result" / "result.json")
        authored = tuple(GraphEdge.model_validate(e) for e in case_result.get("draft_edges", ()))
        assets = validate_assets(
            read_json(root / case["case_dir"] / "result" / "ontology.json"), physical,
            explicit_edges=authored,
        )
        fragments.append((case["source_id"], assets, authored))
    physical_assets = [
        asset
        for kind in PHYSICAL
        for asset in Workspace(physical).read_draft_assets(kind)
    ]
    state = build_incoming_graph(
        load_root_schema(),
        assets=physical_assets,
        cases=fragments,
        catalog_edges=Workspace(physical).read_catalog_edges(),
    )
    validate_graph_state(state, load_root_schema())
    manifest["graph_structure_validation"] = "public_GraphState_schema_passed"
    write_json(root / "manifest.json", manifest)
    return manifest
