from __future__ import annotations

from pathlib import Path

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.workspace import (
    deserialize_asset_envelope,
    serialize_asset_envelope,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.adapter import NodeGraphAdapter
from cortex.capabilities.sql_training.graph.case import project_case_edges
from cortex.capabilities.sql_training.graph.fingerprints import ontology_fingerprint
from cortex.capabilities.sql_training.graph.relationships import (
    compile_all_relationships,
)
from cortex.schema import load_root_schema, validate_graph_state

from .io import fingerprint, read_json, write_json

SEMANTIC = (NodeType.CONCEPT, NodeType.METRIC, NodeType.GENERATION_RULE)
PHYSICAL = (NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE)


def validate_assets(rows, physical_workspace: Path):
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
    ids = {a.id for a in [*physical, *assets]}
    missing = sorted({ref for a in assets for ref in a.references() if ref not in ids})
    if missing:
        raise ValueError(f"unresolved asset references: {missing}")
    compilation = compile_all_relationships(
        [*physical, *assets], endpoint_ids={i: i for i in ids}
    )
    if compilation.diagnostics:
        raise ValueError(
            f"relationship compilation diagnostics: {compilation.diagnostics}"
        )
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
):
    sid = record["source_id"]
    if not sid or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for c in sid
    ):
        raise ValueError(f"unsafe Case ID: {sid}")
    case = root / "cases" / sid
    rows = [serialize_asset_envelope(a) for a in assets]
    edges = project_case_edges(assets)
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
        assets = validate_assets(
            read_json(root / case["case_dir"] / "result" / "ontology.json"), physical
        )
        fragments.append((case["source_id"], assets, project_case_edges(assets)))
    physical_assets = [
        asset
        for kind in PHYSICAL
        for asset in Workspace(physical).read_draft_assets(kind)
    ]
    state, warnings = NodeGraphAdapter(Workspace(physical)).to_trace_incoming_state(
        load_root_schema(),
        committed_assets=physical_assets,
        case_fragments=fragments,
    )
    validate_graph_state(state, load_root_schema())
    if warnings:
        raise ValueError(f"pre-merge projection warnings: {warnings}")
    manifest["graph_structure_validation"] = "public_GraphState_schema_passed"
    write_json(root / "manifest.json", manifest)
    return manifest
