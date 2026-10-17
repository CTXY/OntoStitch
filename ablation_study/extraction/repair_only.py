from __future__ import annotations

import os
from pathlib import Path

import yaml

from cortex.bootstrap.sql_training_ablation import (
    build_repair_only_capability,
    preflight_repair_only,
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
        extraction_max_workers=1,
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


async def run_repair_only(spec: dict, root: Path, workspace: Path):
    # Application's record type is consumed only by Bootstrap's assembled capability boundary.
    from cortex.application.sql_training import QASqlTrainingRecord

    for name, expected in spec.get("physical_hashes", {}).items():
        if sha256(Path(spec["physical_workspace"]) / "assets" / "draft" / name) != expected:
            raise ValueError("frozen physical snapshot changed")

    config = prepare_config(spec, workspace)
    engine = build_repair_only_capability(
        config,
        workspace=workspace / "runtime",
        physical_workspace=Path(spec["physical_workspace"]),
    )
    write_json(workspace / "preflight.json", await preflight_repair_only(engine, config))
    schema = load_root_schema(engine.config.root_schema.external_path)
    cases = []
    lock = fingerprint(spec)
    lock_path = workspace / "experiment_lock.json"
    if lock_path.exists() and read_json(lock_path)["spec_fingerprint"] != lock:
        raise ValueError("runtime workspace belongs to different frozen inputs")
    write_json(lock_path, {"spec_fingerprint": lock})
    existing = {}
    for native_result in (workspace / "runtime").glob("train_runs/*/cases/*/case_result.json"):
        raw = read_json(native_result)
        sid = raw["source_id"]
        if sid in existing:
            raise ValueError(f"multiple native bundles for Case: {sid}")
        existing[sid] = native_result.parent
    remaining = [r for r in spec["records"] if r["source_id"] not in existing]
    if remaining:
        print(f"train_engine_start {spec['database']} cases={len(remaining)}", flush=True)
        _, report = await engine.train(
            tuple(QASqlTrainingRecord.model_validate(r) for r in remaining),
            schema,
            GraphSpace(),
        )
        write_json(workspace / "evaluation_report.json", report.model_dump(mode="json"))
        if report.fatal or len(report.cases) != len(remaining):
            raise RuntimeError(f"training failed before completing frozen Cases: {report.errors}")
        for case in report.cases:
            if not case.bundle_path:
                raise RuntimeError(f"missing native bundle: {case.source_id}")
            existing[case.source_id] = Path(case.bundle_path)
    for record in spec["records"]:
        checkpoint = workspace / "checkpoints" / (record["source_id"] + ".json")
        if checkpoint.exists():
            saved = read_json(checkpoint)
            if saved["spec_fingerprint"] != lock:
                raise ValueError("checkpoint inputs changed")
            raw, rows, provenance = saved["result"], saved["assets"], saved["provenance"]
        else:
            native = existing[record["source_id"]]
            raw = read_json(native / "case_result.json")
            rows = read_json(native / "draft_nodes.json")
            provenance = {
                "native_bundle": str(native),
                "trace_path": str(
                    workspace
                    / "runtime"
                    / "logs"
                    / "train"
                    / (native.parent.parent.name + ".trace.jsonl")
                ),
                "max_rounds": 1,
                "max_iterations": 1,
                "initial_candidates": [],
            }
            write_json(
                checkpoint,
                {"spec_fingerprint": lock, "result": raw, "assets": rows, "provenance": provenance},
            )
            print(
                f"case_complete {record['source_id']} assets={len(rows)} "
                f"correct={raw.get('correct')}",
                flush=True,
            )
        assets = validate_assets(rows, Path(spec["physical_workspace"]))
        cases.append(
            write_case(root, record, assets, arm="repair_only", result=raw, provenance=provenance)
        )
    return finalize_collection(root, spec=spec, arm="repair_only", cases=cases)
