from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from cortex.application import ClarificationAnswer, RetrieveDbPending
from cortex.bootstrap import build_retrieve_db_cli_runtime
from cortex.infrastructure.retrieve import FileSystemGraphSpaceLoader


@click.group("retrieve")
def retrieve_cli() -> None:
    """Retrieve a database semantic subgraph."""


@retrieve_cli.command("db")
@click.argument("question")
@click.option(
    "--run",
    "graph_source",
    required=True,
    type=click.Path(exists=True, readable=True, path_type=Path),
    help="Run directory, inline graph JSON, or legacy graph JSON.",
)
@click.option(
    "--run-id",
    default=None,
    help="Reuse the same ID to recover a durable retrieval run.",
)
@click.option("--dialect", default="mysql", show_default=True)
@click.option(
    "--initial-sql", default=None, help="Optional caller-supplied baseline SQL."
)
@click.option(
    "--enable-clarification/--disable-clarification",
    default=None,
    help="Deprecated compatibility option; user interaction belongs to online-context.",
)
@click.option(
    "--config",
    "config_path",
    default="configs/cortex.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def retrieve_db_cli(
    question: str,
    graph_source: Path,
    run_id: str | None,
    dialect: str,
    initial_sql: str | None,
    enable_clarification: bool | None,
    config_path: Path,
) -> None:
    """Retrieve a semantic subgraph while preserving complete conflict nodes."""
    try:
        graph = FileSystemGraphSpaceLoader().load_state(graph_source)
        runtime = build_retrieve_db_cli_runtime(config_path)
        result = asyncio.run(
            runtime.service.run(
                question=question,
                run_id=run_id,
                graph_state=graph,
                dialect=dialect,
                initial_sql=initial_sql,
                enable_clarification=enable_clarification,
            )
        )
        _publish_db(result.public_payload(), runtime.result_recorder)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@retrieve_cli.command("db-resume")
@click.option(
    "--run",
    "graph_source",
    required=True,
    type=click.Path(exists=True, readable=True, path_type=Path),
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
    "--config",
    "config_path",
    default="configs/cortex.yaml",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def retrieve_db_resume_cli(
    graph_source: Path,
    pending_path: Path,
    answers_path: Path,
    config_path: Path,
) -> None:
    """Resume a DB clarification without repeating signal extraction."""
    try:
        graph = FileSystemGraphSpaceLoader().load_state(graph_source)
        pending = _load_pending(pending_path)
        answers = _load_answers(answers_path)
        runtime = build_retrieve_db_cli_runtime(config_path)
        result = asyncio.run(
            runtime.service.resume(graph=graph.graph, pending=pending, answers=answers)
        )
        _publish_db(result.public_payload(), runtime.result_recorder)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _publish_db(payload: dict, recorder) -> None:
    envelope = {"scenario": "db", **payload}
    output_path = recorder.write(envelope)
    envelope["output"] = str(output_path)
    click.echo(json.dumps(envelope, ensure_ascii=False, indent=2))


def _load_pending(path: Path) -> RetrieveDbPending:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "pending" in payload:
        payload = payload["pending"]
    if payload is None:
        raise ValueError(f"retrieve DB result does not contain pending state: {path}")
    return RetrieveDbPending.model_validate(payload)


def _load_answers(path: Path) -> tuple[ClarificationAnswer, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("answers")
    if not isinstance(payload, list):
        raise TypeError("retrieve DB clarification answers must be a JSON array")
    return tuple(ClarificationAnswer.model_validate(item) for item in payload)


__all__ = ["retrieve_cli"]
