from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import click
from pydantic import ValidationError

from cortex.application import (
    QASqlTrainingRecord,
    RunStatus,
    SqlTrainingResult,
)
from cortex.bootstrap import build_sql_training_service
from cortex.config import load_config
from cortex.infrastructure.artifacts import (
    FileSystemGraphStateRepository,
    write_graph_html_from_json,
)
from cortex.schema import GraphSpace, GraphState, SchemaDefinition, load_root_schema

_OUTPUT_TIMEZONE = ZoneInfo("Asia/Shanghai")


@click.command("sql-train")
@click.option(
    "--config",
    "config_path",
    default="configs/cortex.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Shared Cortex configuration file.",
)
@click.option(
    "--training-set",
    "training_set_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="QA-SQL training set in JSON or JSONL format.",
)
@click.option(
    "--schema",
    "schema_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Schema JSON. Defaults to the root Schema selected by --config.",
)
@click.option(
    "--graph-state",
    "graph_state_path",
    type=click.Path(file_okay=False, path_type=Path),
    help="Persistent GraphStateBundle directory; defaults inside the dated output workspace.",
)
@click.option(
    "--output",
    "output_directory",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Parent directory for a dated SQL Training Workspace.",
)
@click.pass_context
def sql_training_cli(
    ctx: click.Context,
    config_path: Path,
    training_set_path: Path,
    schema_path: Path | None,
    graph_state_path: Path | None,
    output_directory: Path,
) -> None:
    """Train the knowledge graph from a QA-SQL training set."""
    workspace: Path | None = None
    try:
        training_set = _load_training_set(training_set_path)
        schema = _load_schema(config_path, schema_path)
        workspace = _reserve_dated_workspace(output_directory)
        repository = FileSystemGraphStateRepository(
            graph_state_path or workspace / "graph_state",
            schema,
        )
        service = build_sql_training_service(
            config_path,
            workspace=workspace,
            state_repository=repository,
        )
        result = service.run(training_set=training_set, schema=schema)
        _write_result(workspace, result)
        _write_case_review_index(workspace, result)
        if result.status != RunStatus.FAILED:
            _write_graph(workspace, result.state.graph)
            graph_state_path = _write_graph_state(workspace, result.state)
            write_graph_html_from_json(
                graph_state_path,
                workspace / "graph.html",
                schema,
                title="SQL Training 图谱",
            )
    except click.ClickException:
        raise
    except Exception as exc:
        _remove_empty_workspace(workspace)
        raise click.ClickException(str(exc)) from exc

    click.echo(
        json.dumps(
            {
                "status": result.status.value,
                "output": str(workspace),
                "nodes": len(result.state.graph.nodes),
                "edges": len(result.state.graph.edges),
                "passed": result.evaluation_report.passed,
                "failed": result.evaluation_report.failed,
            },
            ensure_ascii=False,
        )
    )
    if result.status == RunStatus.FAILED:
        raise click.ClickException(f"SQL training failed; inspect {workspace}")
    if result.status == RunStatus.NEEDS_REVIEW:
        ctx.exit(2)


def _load_training_set(path: Path) -> tuple[QASqlTrainingRecord, ...]:
    payload = (
        _read_jsonl(path)
        if path.suffix.lower() in {".jsonl", ".ndjson"}
        else _read_json(path)
    )
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("training_set"), list):
        rows = payload["training_set"]
    elif isinstance(payload, dict) and isinstance(payload.get("queries"), list):
        rows = payload["queries"]
    else:
        raise ValueError(
            f"{path}: expected a JSON array or an object containing training_set or queries"
        )

    records: list[QASqlTrainingRecord] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: training record {index} must be a JSON object")
        try:
            records.append(
                QASqlTrainingRecord.model_validate(_normalize_record(row, index))
            )
        except ValidationError as exc:
            raise ValueError(f"{path}: invalid training record {index}: {exc}") from exc
    return tuple(records)


def _normalize_record(row: dict[str, Any], index: int) -> dict[str, Any]:
    if "question" in row or "sql" in row:
        normalized = dict(row)
        normalized.setdefault("source_id", row.get("id") or f"case-{index:04d}")
        normalized.pop("id", None)
        return normalized

    optional_fields = (
        "generated_sql",
        "assets",
        "metrics",
        "semantic_mappings",
        "generation_rules",
    )
    normalized = {
        "source_id": row.get("source_id") or row.get("id") or f"case-{index:04d}",
        "question": row.get("query"),
        "sql": row.get("gt_sql"),
    }
    normalized.update({key: row[key] for key in optional_fields if key in row})
    return normalized


def _load_schema(config_path: Path, schema_path: Path | None) -> SchemaDefinition:
    if schema_path is not None:
        return load_root_schema(schema_path)
    config = load_config(config_path)
    return load_root_schema(config.root_schema.external_path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc


def _read_jsonl(path: Path) -> list[Any]:
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
    return rows


def _reserve_dated_workspace(
    output_directory: Path,
    *,
    started_at: datetime | None = None,
) -> Path:
    storage_root = output_directory.expanduser().resolve()
    if storage_root.exists() and not storage_root.is_dir():
        raise ValueError(f"SQL training output must be a directory: {storage_root}")
    candidate = (started_at or _now()).astimezone(_OUTPUT_TIMEZONE)
    candidate = candidate.replace(microsecond=(candidate.microsecond // 1000) * 1000)
    while True:
        workspace = storage_root / _dated_workspace_id(candidate)
        try:
            workspace.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            candidate += timedelta(milliseconds=1)
            continue
        except OSError as exc:
            raise ValueError(
                f"unable to create SQL training workspace {workspace}: {exc}"
            ) from exc
        return workspace


def _dated_workspace_id(value: datetime) -> str:
    local = value.astimezone(_OUTPUT_TIMEZONE)
    return f"{local:%Y%m%d-%H%M%S}-{local.microsecond // 1000:03d}"


def _now() -> datetime:
    return datetime.now(_OUTPUT_TIMEZONE)


def _remove_empty_workspace(workspace: Path | None) -> None:
    if workspace is None:
        return
    try:
        workspace.rmdir()
    except OSError:
        # Preserve any runtime artifacts already written before the failure.
        pass


def _write_graph(workspace: Path, graph: GraphSpace) -> Path:
    target = workspace / "graph.json"
    content = graph.model_dump_json(indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".graph.json.",
        suffix=".tmp",
        dir=workspace,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _write_graph_state(workspace: Path, state: GraphState) -> Path:
    target = workspace / "graph_state.json"
    content = state.model_dump_json(indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".graph_state.json.",
        suffix=".tmp",
        dir=workspace,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _write_result(workspace: Path, result: SqlTrainingResult) -> Path:
    """Persist the single formal SQL Training result for every terminal status."""

    target = workspace / "result.json"
    content = result.model_dump_json(indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".result.json.",
        suffix=".tmp",
        dir=workspace,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _write_case_review_index(workspace: Path, result: SqlTrainingResult) -> Path:
    """Write a compact, one-row-per-case review entry beside the final graph.

    The linked bundle contains the complete per-case draft ontology and the
    runtime workspace's ``train_runs/.../extraction_graphs`` directory contains
    the pre-merge extraction snapshot.  Keeping this index at the CLI artifact
    boundary lets reviewers inspect an interrupted or needs-review run without
    parsing the large trace JSONL file.
    """

    rows = []
    for case in result.evaluation_report.cases:
        rows.append(
            {
                "source_id": case.source_id,
                "status": case.training_status,
                "stable_correct": case.completion_reason == "stable_correct",
                "passed": case.passed,
                "failed_closed": case.failed_closed,
                "fatal": case.fatal,
                "replay_status": case.replay_status,
                "required_correct_replays": case.required_correct_replays,
                "completed_replays": case.completed_replays,
                "active_targets_complete": case.active_targets_complete,
                "residual_status": case.residual_status,
                "case_stable_correct": case.case_stable_correct,
                "message": case.message,
                "bundle_path": case.bundle_path,
                "draft_node_count": case.draft_node_count,
                "draft_node_ids": list(case.draft_node_ids),
                "accepted_metric_ids": list(case.accepted_metric_ids),
                "accepted_generation_rule_ids": list(case.accepted_generation_rule_ids),
                "committed_semantic_mapping_ids": list(
                    case.committed_semantic_mapping_ids
                ),
            }
        )
    target = workspace / "case_review.jsonl"
    temporary = workspace / ".case_review.jsonl.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return target


__all__ = ["sql_training_cli"]
