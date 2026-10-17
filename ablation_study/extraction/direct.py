from pathlib import Path

from .bundles import finalize_collection, validate_assets, write_case
from .io import read_json, sha256


def export_direct(spec: dict, root: Path):
    for name, expected in spec.get("physical_hashes", {}).items():
        if sha256(Path(spec["physical_workspace"]) / "assets" / "draft" / name) != expected:
            raise ValueError("frozen physical snapshot changed")
    cases = []
    for record in spec["records"]:
        source = spec["sources"][record["source_id"]]
        path = Path(source["snapshot"])
        if sha256(path) != source["sha256"]:
            raise ValueError(f"frozen extraction snapshot changed: {path}")
        snapshot = read_json(path)
        if snapshot.get("stage") != "post_extraction_pre_merge":
            raise ValueError(f"unexpected extraction stage: {path}")
        ids = snapshot["candidate_node_ids"]
        rows = [r for r in snapshot["nodes"] if r["id"] in ids]
        if len(rows) != len(ids):
            raise ValueError("snapshot candidate IDs do not match nodes")
        assets = validate_assets(rows, Path(spec["physical_workspace"]))
        result = {
            "source_id": record["source_id"],
            "correct": None,
            "asset_outcome": "unassessed" if assets else "empty",
            "extraction_status": snapshot["status"],
            "error": snapshot.get("error"),
            "initial_assessment_performed": False,
            "repair_performed": False,
        }
        cases.append(
            write_case(root, record, assets, arm="direct", result=result, provenance=source)
        )
    return finalize_collection(root, spec=spec, arm="direct", cases=cases)
