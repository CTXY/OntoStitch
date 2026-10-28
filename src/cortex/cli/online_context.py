from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click

from cortex.application.online_context import InteractionAnswer, OnlineContextPending
from cortex.bootstrap import build_online_context_service
from cortex.config import load_config
from cortex.infrastructure.online_context import (
    DatabaseDescriptionLoader,
    FileSystemGraphStateLoader,
)


@click.group("online-context")
def online_context_cli() -> None:
    """Build request-local online context from a complete GraphState."""


@online_context_cli.command("run")
@click.argument("question")
@click.option(
    "--graph-state",
    "graph_state_source",
    required=True,
    type=click.Path(exists=True, readable=True, path_type=Path),
    help="SQL Training run directory or graph_state.json file.",
)
@click.option(
    "--initial-sql", default=None, help="Optional caller-supplied candidate SQL."
)
@click.option(
    "--run-id",
    default=None,
    help="Stable alphanumeric session ID for retrying failed runs.",
)
@click.option(
    "--question-budget",
    type=click.IntRange(min=0),
    default=None,
    help="Maximum clarification questions, including JOINs. Overrides configuration.",
)
@click.option(
    "--schema-context",
    "--column-context-dir",
    "column_context_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False, readable=True, path_type=Path),
    help="Optional BIRD-style database_description directory.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Optional JSON output path.",
)
@click.option(
    "--trace-output",
    "trace_output_path",
    default=None,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Optional organizer trace JSON output path.",
)
@click.option(
    "--config",
    "config_path",
    default="configs/cortex.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Optional configuration override.",
)
def online_context_run_cli(
    question: str,
    graph_state_source: Path,
    initial_sql: str | None,
    run_id: str | None,
    question_budget: int | None,
    column_context_dir: Path | None,
    output_path: Path | None,
    trace_output_path: Path | None,
    config_path: Path,
) -> None:
    """Run Online Context for one natural-language question."""
    try:
        config = load_config(config_path)
        graph_state = FileSystemGraphStateLoader().load(graph_state_source)
        column_context = (
            DatabaseDescriptionLoader().load(column_context_dir)
            if column_context_dir is not None
            else ()
        )
        traces: list[dict[str, Any]] = []
        trace_sink = (
            (lambda stage, payload: traces.append({"stage": stage, "payload": payload}))
            if trace_output_path is not None
            else None
        )
        service = build_online_context_service(
            config_path,
            config=config,
            trace_sink=trace_sink,
        )
        result = asyncio.run(
            service.run(
                question=question,
                graph_state=graph_state,
                dialect=_dialect_from_config(config),
                initial_sql=initial_sql,
                column_context=column_context,
                question_budget=question_budget,
                run_id=run_id,
            )
        )
        payload = _public_payload(result.public_payload())
        _write_optional(trace_output_path, {"traces": traces})
        _publish(payload, output_path)
    except click.ClickException:
        raise
    except Exception as exc:
        notes = getattr(exc, "__notes__", ())
        raise click.ClickException("\n".join((str(exc), *notes))) from exc


@online_context_cli.command("resume")
@click.option(
    "--graph-state",
    "graph_state_source",
    required=True,
    type=click.Path(exists=True, readable=True, path_type=Path),
    help="SQL Training run directory or graph_state.json file.",
)
@click.option(
    "--pending",
    "pending_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--answers",
    "answers_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "--output",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    help="Optional JSON output path.",
)
@click.option(
    "--config",
    "config_path",
    default="configs/cortex.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
    help="Optional configuration override.",
)
def online_context_resume_cli(
    graph_state_source: Path,
    pending_path: Path,
    answers_path: Path,
    output_path: Path | None,
    config_path: Path,
) -> None:
    """Resume Online Context after collecting user answers."""
    try:
        config = load_config(config_path)
        graph_state = FileSystemGraphStateLoader().load(graph_state_source)
        pending = _load_pending(pending_path)
        answers = _load_answers(answers_path)
        service = build_online_context_service(config_path, config=config)
        result = asyncio.run(
            service.resume(
                graph_state=graph_state,
                pending=pending,
                answers=answers,
            )
        )
        _publish(_public_payload(result.public_payload()), output_path)
    except click.ClickException:
        raise
    except Exception as exc:
        notes = getattr(exc, "__notes__", ())
        raise click.ClickException("\n".join((str(exc), *notes))) from exc


def _dialect_from_config(config) -> str:
    database_type = str(getattr(config.database, "type", "") or "").strip()
    return database_type or "mysql"


def _public_payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("pending") is not None:
        return {
            "status": "needs_user_input",
            "interaction_questions": result.get("interaction_questions", []),
            "pending": result["pending"],
            "run_id": result.get("run_id"),
            "relevant_graph": result.get("relevant_graph"),
        }
    payload = {
        "status": result["status"],
        "run_id": result.get("run_id"),
        "relevant_graph": result.get("relevant_graph"),
        "unresolved_choice_point_ids": result.get("unresolved_choice_point_ids", []),
        "projection_trace": result.get("projection_trace", []),
    }
    guidance = result.get("downstream_guidance")
    if guidance is not None:
        public_guidance = {"guidance_content": guidance.get("guidance_content", "")}
        if guidance.get("query_examples"):
            public_guidance["query_examples"] = guidance["query_examples"]
        payload["downstream_guidance"] = public_guidance
    return payload


def _load_pending(path: Path) -> OnlineContextPending:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "pending" in payload:
        payload = payload["pending"]
    if payload is None:
        raise ValueError(
            f"online context result does not contain pending state: {path}"
        )
    return OnlineContextPending.model_validate(payload)


def _load_answers(path: Path) -> tuple[InteractionAnswer, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("answers")
    if not isinstance(payload, list):
        raise TypeError("online context answers must be a JSON array")
    return tuple(InteractionAnswer.model_validate(item) for item in payload)


def _publish(payload: dict[str, Any], output_path: Path | None) -> None:
    if output_path is not None:
        _write_optional(output_path, payload)
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _write_optional(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = ["online_context_cli"]
