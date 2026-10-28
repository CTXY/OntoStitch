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
    TrainingExtractionResult,
    extract_training,
    merge_training_extraction,
)
from cortex.bootstrap import (
    build_graph_merge_operator,
    build_sql_extraction,
    build_sql_training_service,
)
from cortex.capabilities.sql_training.artifacts.workspace import Workspace
from cortex.capabilities.sql_training.graph_merge_evidence import (
    SqlTrainingGraphMergeEvidenceResolver,
    WorkspaceEvidenceRepository,
)
from cortex.config import config_fingerprint, load_config
from cortex.infrastructure.artifacts import (
    FileSystemGraphMergeTraceSink,
    FileSystemGraphStateRepository,
    write_graph_html_from_json,
)
from cortex.infrastructure.progress import emit_progress, progress_file
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
@click.option(
    "--progress-file",
    "progress_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Append machine-readable progress events (optional).",
)
@click.option(
    "--result-file",
    "result_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the terminal run summary atomically (optional).",
)
@click.option("--run-id", help="Stable Graph Merge run ID for checkpoint recovery.")
@click.pass_context
def sql_training_cli(
    ctx: click.Context,
    config_path: Path,
    training_set_path: Path,
    schema_path: Path | None,
    graph_state_path: Path | None,
    output_directory: Path,
    progress_path: Path | None = None,
    result_path: Path | None = None,
    run_id: str | None = None,
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
        with progress_file(progress_path):
            emit_progress("phase", phase="initializing", total=len(training_set))
            service = build_sql_training_service(
                config_path,
                workspace=workspace,
                state_repository=repository,
            )
            result = service.run(
                training_set=training_set, schema=schema, run_id=run_id
            )
            emit_progress("phase", phase="exporting")
            _export_training_result(workspace, result, schema)
            emit_progress("finished", status=result.status.value)
    except click.ClickException:
        raise
    except Exception as exc:
        _remove_empty_workspace(workspace)
        raise click.ClickException(str(exc)) from exc

    _finish(ctx, result.status, _summary(workspace, result), result_path)


def _offline_options(command):
    """Shared flags for the two independently callable offline stages."""
    options = [
        click.option(
            "--config",
            "config_path",
            default="configs/cortex.yaml",
            show_default=True,
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
        ),
        click.option(
            "--schema",
            "schema_path",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
        ),
        click.option(
            "--output",
            "output_directory",
            required=True,
            type=click.Path(file_okay=False, path_type=Path),
        ),
        click.option(
            "--graph-state",
            "graph_state_path",
            type=click.Path(file_okay=False, path_type=Path),
            help="Persistent graph bundle; extraction reads it, merge updates it.",
        ),
        click.option(
            "--existing-state",
            "existing_state_path",
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            help="Exported base GraphState JSON; mutually exclusive with --graph-state.",
        ),
        click.option(
            "--result-file",
            "result_path",
            type=click.Path(dir_okay=False, path_type=Path),
        ),
        click.option(
            "--progress-file",
            "progress_path",
            type=click.Path(dir_okay=False, path_type=Path),
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


@click.command("sql-extract")
@_offline_options
@click.option(
    "--training-set",
    "training_set_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.pass_context
def sql_extraction_cli(
    ctx,
    config_path,
    schema_path,
    output_directory,
    graph_state_path,
    existing_state_path,
    result_path,
    progress_path,
    training_set_path,
):
    """Extract a portable training result without running Graph Merge."""
    workspace = None
    try:
        records = _load_training_set(training_set_path)
        schema = _load_schema(config_path, schema_path)
        existing = _load_existing_state(schema, graph_state_path, existing_state_path)
        workspace = _reserve_dated_workspace(output_directory)
        with progress_file(progress_path):
            engine = build_sql_extraction(config_path, workspace=workspace)
            extraction = extract_training(engine, records, schema, existing)
            _write_model(workspace / "extraction_result.json", extraction)
            # Human-readable views; only extraction_result.json is the merge handoff.
            _write_model(workspace / "incoming_state.json", extraction.state)
            _write_model(workspace / "incoming_graph.json", extraction.state.graph)
            _write_model(
                workspace / "evaluation_report.json", extraction.evaluation_report
            )
            summary = {
                "status": extraction.status.value,
                "output": str(workspace),
                "workspace": str(workspace),
                "merge_performed": False,
                "training_records": len(records),
                "nodes": len(extraction.state.graph.nodes),
                "edges": len(extraction.state.graph.edges),
                "passed": extraction.evaluation_report.passed,
                "failed": extraction.evaluation_report.failed,
                "trace_path": extraction.evaluation_report.trace_path,
            }
            _write_json(workspace / "premerge_summary.json", summary)
            emit_progress("finished", status=extraction.status.value)
    except Exception as exc:
        _remove_empty_workspace(workspace)
        raise click.ClickException(str(exc)) from exc
    _finish(ctx, extraction.status, summary, result_path)


@click.command("graph-merge")
@_offline_options
@click.option(
    "--training-workspace",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--run-id", help="Resume the same merge using merge_run_id.txt.")
@click.pass_context
def graph_merge_cli(
    ctx,
    config_path,
    schema_path,
    output_directory,
    graph_state_path,
    existing_state_path,
    result_path,
    progress_path,
    training_workspace,
    run_id,
):
    """Merge a training extraction into its base graph and export the final result."""
    try:
        config = load_config(config_path)
        schema = _load_schema(config_path, schema_path)
        # Fail closed: never infer extraction success from an incoming graph alone.
        handoff = training_workspace / "extraction_result.json"
        if not handoff.is_file():
            raise ValueError(
                "Missing extraction_result.json; rerun sql-extract to produce a complete handoff"
            )
        extraction = TrainingExtractionResult.model_validate_json(
            handoff.read_text(encoding="utf-8")
        )
        if graph_state_path is not None and existing_state_path is not None:
            raise ValueError(
                "--graph-state and --existing-state are mutually exclusive"
            )
        if existing_state_path is not None:
            existing = _load_existing_state(schema, None, existing_state_path)
            if existing != extraction.existing_state:
                raise ValueError(
                    "--existing-state does not match extraction existing_state; rerun extraction with this base"
                )
        output = output_directory.expanduser().resolve()
        marker = output / "merge_run_id.txt"
        if output.exists():
            if (
                not run_id
                or not marker.is_file()
                or marker.read_text().strip() != run_id
            ):
                raise ValueError(
                    "Existing output requires --run-id matching merge_run_id.txt"
                )
        else:
            output.mkdir(parents=True)
        from uuid import uuid4

        run_id = run_id or uuid4().hex
        marker.write_text(run_id + "\n", encoding="utf-8")
        with progress_file(progress_path):
            # Construction happens inside the shared gate, after input validation.
            def build_operator():
                return build_graph_merge_operator(
                    config,
                    evidence_resolver=SqlTrainingGraphMergeEvidenceResolver(
                        WorkspaceEvidenceRepository(Workspace(training_workspace))
                    ),
                    trace_sink=FileSystemGraphMergeTraceSink(
                        output / "llm_traces.jsonl"
                    ),
                )

            repository = (
                FileSystemGraphStateRepository(graph_state_path, schema)
                if graph_state_path is not None
                else None
            )
            result = merge_training_extraction(
                extraction,
                None,
                schema,
                graph_merge_factory=build_operator,
                state_repository=repository,
                config_fingerprint=config_fingerprint(config),
                run_id=run_id,
            )
            _export_training_result(output, result, schema)
            emit_progress("finished", status=result.status.value)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    _finish(ctx, result.status, _summary(output, result), result_path)


def _load_existing_state(schema, graph_state_path=None, existing_state_path=None):
    from cortex.application.ontology_graph import GraphManager

    if graph_state_path is not None and existing_state_path is not None:
        raise ValueError("--graph-state and --existing-state are mutually exclusive")
    if existing_state_path is not None:
        state = GraphState.model_validate_json(
            existing_state_path.read_text(encoding="utf-8")
        )
    elif graph_state_path is not None:
        state = FileSystemGraphStateRepository(graph_state_path, schema).load()
    else:
        state = GraphState(graph=GraphSpace())
    return GraphManager(schema).validate_state(state)


def _summary(workspace: Path, result: SqlTrainingResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "output": str(workspace),
        "nodes": len(result.state.graph.nodes),
        "edges": len(result.state.graph.edges),
        "passed": result.evaluation_report.passed,
        "failed": result.evaluation_report.failed,
        "merge_run_id": result.merge_report.run_id if result.merge_report else None,
    }


def _finish(ctx, status, summary, result_path=None):
    if result_path is not None:
        _write_json(result_path, summary)
    click.echo(json.dumps(summary, ensure_ascii=False))
    if status == RunStatus.FAILED:
        raise click.ClickException(f"Offline stage failed; inspect {summary['output']}")
    if status == RunStatus.NEEDS_REVIEW:
        ctx.exit(2)


def _export_training_result(workspace, result, schema):
    _write_result(workspace, result)
    _write_case_review_index(workspace, result)
    if result.merge_report is not None:
        _write_model(workspace / "merge_report.json", result.merge_report)
    if result.status != RunStatus.FAILED:
        _write_graph(workspace, result.state.graph)
        path = _write_graph_state(workspace, result.state)
        write_graph_html_from_json(
            path, workspace / "graph.html", schema, title="SQL Training 图谱"
        )
    else:
        # A failed resume must not leave exports from an earlier successful attempt.
        for name in ("graph.json", "graph_state.json", "graph.html"):
            (workspace / name).unlink(missing_ok=True)
    if result.merge_report is None:
        (workspace / "merge_report.json").unlink(missing_ok=True)


def _write_model(path, model):
    _atomic_write(path, model.model_dump_json(indent=2) + "\n")


def _write_json(path, payload):
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


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
        raise ValueError(  # noqa: TRY004 -- invalid serialized input
            f"{path}: expected a JSON array or an object containing training_set or queries"
        )

    records: list[QASqlTrainingRecord] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"{path}: training record {index} must be a JSON object")  # noqa: TRY004
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
        "evidence",
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
    _write_model(target, graph)
    return target


def _write_graph_state(workspace: Path, state: GraphState) -> Path:
    target = workspace / "graph_state.json"
    _write_model(target, state)
    return target


def _write_result(workspace: Path, result: SqlTrainingResult) -> Path:
    """Persist the single formal SQL Training result for every terminal status."""
    target = workspace / "result.json"
    _write_model(target, result)
    return target


def _write_case_review_index(workspace: Path, result: SqlTrainingResult) -> Path:
    """Write a compact, one-row-per-case review entry beside the final graph.

    The linked result/ontology.json contains the final semantic assets and the
    case workspace's ``ontology/<revision>.json`` files contain
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
                "result_path": case.result_path,
                "ontology_path": case.ontology_path,
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


__all__ = ["graph_merge_cli", "sql_extraction_cli", "sql_training_cli"]
