"""Run SQL Training through the Engine boundary and stop before Graph Merge.

This is an experiment runner, not a second training implementation. Bootstrap still
assembles the production SQL Training Engine; this script deliberately invokes only
the Engine contract so reviewers can inspect case-local artifacts before an explicit
Graph Merge run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cortex.application import QASqlTrainingRecord
from cortex.bootstrap import build_sql_training_service
from cortex.config import load_config
from cortex.infrastructure.artifacts import write_graph_html_from_json
from cortex.schema import GraphSpace, load_root_schema


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--training-set", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _load_records(path: Path) -> tuple[QASqlTrainingRecord, ...]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return tuple(QASqlTrainingRecord.model_validate(row) for row in rows)


def _dated_workspace(parent: Path) -> Path:
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    workspace = (parent / timestamp).resolve()
    workspace.mkdir(parents=True, exist_ok=False)
    return workspace


async def _run(args: argparse.Namespace) -> dict[str, object]:
    workspace = _dated_workspace(args.output)
    config = load_config(args.config)
    schema = load_root_schema(config.root_schema.external_path)
    records = _load_records(args.training_set)
    service = build_sql_training_service(args.config, workspace=workspace)

    # Bootstrap owns construction; stopping at the Engine contract is the intentional
    # pre-merge boundary. SqlTrainingService.run() is deliberately not invoked.
    incoming_state, report = await service._engine.train(records, schema, GraphSpace())

    graph_path = workspace / "incoming_graph.json"
    graph_path.write_text(incoming_state.graph.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (workspace / "incoming_state.json").write_text(
        incoming_state.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (workspace / "evaluation_report.json").write_text(
        report.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    write_graph_html_from_json(
        graph_path,
        workspace / "incoming_graph.html",
        schema,
        title="SQL Training · Pre-Merge Incoming Graph",
    )
    summary = {
        "status": "pre_merge_review",
        "merge_performed": False,
        "workspace": str(workspace),
        "training_records": len(records),
        "nodes": len(incoming_state.graph.nodes),
        "edges": len(incoming_state.graph.edges),
        "passed": report.passed,
        "failed": report.failed,
        "trace_path": report.trace_path,
    }
    (workspace / "premerge_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    print(json.dumps(asyncio.run(_run(_arguments())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
