from pathlib import Path

from cortex.capabilities.sql_training.artifacts.workspace import (
    serialize_asset_envelope,
)
from cortex.capabilities.sql_training.graph.snapshots import GraphSnapshot

from .bundles import finalize_collection, validate_assets, write_case
from .io import read_json, sha256


def export_direct(spec: dict, root: Path):
    for name, expected in spec.get("physical_hashes", {}).items():
        if (
            sha256(Path(spec["physical_workspace"]) / "assets" / "draft" / name)
            != expected
        ):
            raise ValueError("frozen physical snapshot changed")
    cases = []
    for case_index, record in enumerate(spec["records"], 1):
        source = spec["sources"][record["source_id"]]
        path = Path(source["snapshot"])
        if sha256(path) != source["sha256"]:
            raise ValueError(f"frozen extraction snapshot changed: {path}")
        snapshot = GraphSnapshot.model_validate(read_json(path))
        rows = [serialize_asset_envelope(asset) for asset in snapshot.semantic_assets]
        assets = validate_assets(rows, Path(spec["physical_workspace"]), explicit_edges=snapshot.explicit_edges)
        result = {
            "source_id": record["source_id"],
            "correct": None,
            "asset_outcome": "unassessed" if assets else "empty",
            "extraction_status": "completed",
            "initial_assessment_performed": False,
            "repair_performed": False,
        }
        cases.append(
            write_case(
                root,
                record,
                assets,
                arm="direct",
                result=result,
                provenance=source,
                case_index=case_index,
                physical_workspace=Path(spec["physical_workspace"]),
                explicit_edges=snapshot.explicit_edges,
            )
        )
    return finalize_collection(root, spec=spec, arm="direct", cases=cases)
