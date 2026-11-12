from __future__ import annotations

import os
from pathlib import Path

import yaml

from cortex.bootstrap.sql_training_ablation import (
    build_repair_only_capability,
    preflight_repair_only,
)
from cortex.capabilities.sql_training.artifacts.cases import load_case_assets
from cortex.capabilities.sql_training.artifacts.workspace import (
    serialize_asset_envelope,
)
from cortex.schema import GraphSpace, load_root_schema

from .bundles import finalize_collection, validate_assets, write_case
from .io import fingerprint, read_json, sha256, write_json


def prepare_config(spec: dict, workspace: Path):
    source = Path(spec["config"])
    if not source.is_file():
        raise FileNotFoundError(f"missing source runtime configuration: {source}")
    payload = yaml.safe_load(source.read_text())
    training = payload.setdefault("services", {}).setdefault("sql_training", {})
    worker_limit = int(os.environ.get("CORTEX_ABLATION_CASE_MAX_WORKERS", "4"))
    if worker_limit < 1:
        raise ValueError("ablation Case workers must be positive")
    training.update(
        max_rounds=1,
        max_iterations=1,
        case_max_workers=worker_limit,
        trace_level="info",
    )
    database = payload["database"]
    if database.get("type") == "sqlite":
        database["overwrite"] = False
        # Training must use the existing database without reinitialization.
        database["schema_path"] = None
        database["seed_path"] = None
    target = workspace / "cortex.run.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
    return target


def run_repair_only(spec: dict, root: Path, workspace: Path):
    # Application's record type is consumed only by Bootstrap's assembled capability boundary.
    from cortex.application.sql_training import QASqlTrainingRecord

    for name, expected in spec.get("physical_hashes", {}).items():
        if (
            sha256(Path(spec["physical_workspace"]) / "assets" / "draft" / name)
            != expected
        ):
            raise ValueError("frozen physical snapshot changed")

    config = prepare_config(spec, workspace)
    engine = build_repair_only_capability(
        config,
        workspace=workspace / "runtime",
        physical_workspace=Path(spec["physical_workspace"]),
    )
    write_json(workspace / "preflight.json", preflight_repair_only(engine, config))
    schema = load_root_schema(engine.config.root_schema.external_path)
    cases = []
    lock = fingerprint(spec)
    lock_path = workspace / "experiment_lock.json"
    if lock_path.exists() and read_json(lock_path)["spec_fingerprint"] != lock:
        raise ValueError("runtime workspace belongs to different frozen inputs")
    write_json(lock_path, {"spec_fingerprint": lock})
    existing = {}
    for native_case in (workspace / "runtime").glob("train_runs/*/cases/*/case.json"):
        sid = read_json(native_case)["source"]["source_id"]
        if sid in existing:
            raise ValueError(f"multiple native cases for source: {sid}")
        native_result = native_case.parent / "result" / "result.json"
        if not native_result.is_file():
            raise RuntimeError(
                f"unfinished native case {sid}; resume it through TrainPipeline.resume_case before exporting"
            )
        existing[sid] = native_result
    remaining = [r for r in spec["records"] if r["source_id"] not in existing]
    if remaining:
        print(
            f"train_engine_start {spec['database']} cases={len(remaining)}", flush=True
        )
        _, report = engine.train(
            tuple(QASqlTrainingRecord.model_validate(r) for r in remaining),
            schema,
            GraphSpace(),
        )
        write_json(workspace / "evaluation_report.json", report.model_dump(mode="json"))
        if report.fatal or len(report.cases) != len(remaining):
            raise RuntimeError(
                f"training failed before completing frozen Cases: {report.errors}"
            )
        for case in report.cases:
            if not case.result_path or not Path(case.result_path).is_file():
                raise RuntimeError(f"missing native result: {case.source_id}")
            existing[case.source_id] = Path(case.result_path)
    for case_index, record in enumerate(spec["records"], 1):
        native_result = existing[record["source_id"]]
        native_case = native_result.parent.parent
        raw = read_json(native_result)
        rows = [
            serialize_asset_envelope(a)
            for a in load_case_assets(raw, case_root=native_case)
        ]
        info = read_json(native_case / "case.json")
        provenance = {
            "native_case": str(native_case),
            "trace_path": str(
                workspace
                / "runtime"
                / "logs"
                / "train"
                / (info["run_id"] + ".trace.jsonl")
            ),
            "max_rounds": 1,
            "max_iterations": 1,
        }
        from cortex.schema import GraphEdge
        assets = validate_assets(rows, Path(spec["physical_workspace"]), explicit_edges=[GraphEdge.model_validate(e) for e in raw.get("draft_edges", ())])
        cases.append(
            write_case(
                root,
                record,
                assets,
                arm="repair_only",
                result=raw,
                provenance=provenance,
                case_index=case_index,
                physical_workspace=Path(spec["physical_workspace"]),
            )
        )
        print(
            f"case_complete {record['source_id']} assets={len(rows)} correct={raw.get('correct')}",
            flush=True,
        )
    return finalize_collection(root, spec=spec, arm="repair_only", cases=cases)
