"""Standalone M0 CLI; semantic interpretation is explicitly opt-in."""

import importlib
import json
from pathlib import Path
from uuid import uuid4

import click

from cortex.bootstrap.metadata import build_metadata_service
from cortex.capabilities.metadata.models import BuildOptions
from cortex.capabilities.metadata.views import MetadataReader


@click.group("metadata")
def metadata_cli():
    """Build, inspect and export database metadata M0 (no case learning)."""


@metadata_cli.command("build")
@click.option(
    "--database",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--workspace", required=True, type=click.Path(path_type=Path))
@click.option(
    "--initial-file", multiple=True, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "--agent/--no-agent",
    default=False,
    help="Enable database semantic enrichment after initial-file loading.",
)
@click.option(
    "--config", default="configs/cortex.yaml", type=click.Path(path_type=Path)
)
@click.option(
    "--file-mode",
    type=click.Choice(["loader", "agent"]),
    default="loader",
    help="File import: dataset-specific loader (BIRD includes LLM interpretation), or autonomous Agent.",
)
@click.option("--loader", help="bird, normalized (default), or python.module:factory")
@click.option(
    "--context",
    help="Optional domain context, supplied to the Agent as a hint rather than verified evidence.",
)
@click.option("--profile-rows", default=10000, type=click.IntRange(1, 1000000))
@click.option(
    "--query-timeout",
    default=10.0,
    type=click.FloatRange(min=0, min_open=True, max=300),
)
@click.option(
    "--max-agent-calls",
    default=60,
    type=click.IntRange(1, 100),
    help="Model-call budget per Agent run (file interpretation or database enrichment).",
)
@click.option(
    "--expected-revision",
    default=0,
    type=click.IntRange(min=0),
    help="0 creates M0; explicitly supply the current revision to rebuild.",
)
def build(
    database,
    workspace,
    initial_file,
    agent,
    config,
    file_mode,
    loader,
    context,
    profile_rows,
    query_timeout,
    max_agent_calls,
    expected_revision,
):
    """Scan DDL/data, import optional descriptions, optionally run the Agent."""
    service = None
    snapshot = None
    failure = None
    attempt = uuid4().hex
    try:
        options = BuildOptions(
            profile_rows=profile_rows,
            query_timeout=query_timeout,
            max_agent_calls=max_agent_calls,
            context=context,
        )
        model = None
        if agent or file_mode == "agent" or loader == "bird":
            from cortex.config import load_config
            from cortex.infrastructure.llm import create_chat_model

            model = create_chat_model(load_config(config).llm)
        custom = loader
        if custom and ":" in custom:
            module, factory = custom.split(":", 1)
            custom = getattr(importlib.import_module(module), factory)()
        service = build_metadata_service(
            workspace,
            model=model,
            options=options,
            loader=custom,
            file_mode=file_mode,
            enrich_database=agent,
        )
        snapshot = service.build(
            database,
            initial_files=initial_file,
            expected_revision=expected_revision,
        )
        click.echo(
            json.dumps(
                {
                    "revision": snapshot.revision,
                    "tables": len(snapshot.tables),
                    "issues": snapshot.issues,
                    "repository": str(workspace / "metadata.sqlite"),
                },
                ensure_ascii=False,
            )
        )
    except Exception as exc:
        failure = str(exc)
        raise click.ClickException(str(exc)) from exc
    finally:
        if service is not None and service.runs:
            for stage, result in service.runs:
                name = {"files": "file-agent"}.get(stage, "agent")
                suffix = (
                    f"r{snapshot.revision}"
                    if snapshot is not None
                    else f"failed-{attempt}"
                )
                report = {
                    "stage": stage,
                    "status": result.status,
                    "published": snapshot is not None,
                    "error": result.error or failure,
                    "trace": result.trace,
                }
                try:
                    workspace.mkdir(parents=True, exist_ok=True)
                    (workspace / f"{name}-trace-{suffix}.json").write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except OSError as exc:
                    click.echo(f"Could not save Agent trace: {exc}", err=True)


@metadata_cli.command("show")
@click.option(
    "--workspace", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--revision", type=click.IntRange(min=1))
@click.option("--object-id")
@click.option("--offset", default=0, type=click.IntRange(min=0))
@click.option("--limit", default=20, type=click.IntRange(1, 50))
def show(workspace, revision, object_id, offset, limit):
    """Read a revision or one table/column/database object as JSON."""
    try:
        service = build_metadata_service(workspace)
        result = (
            service.inspect(
                object_id, revision=revision, offset=offset, limit=limit
            ).model_dump(mode="json")
            if object_id
            else service.export(revision)
        )
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, LookupError) as exc:
        raise click.ClickException(str(exc)) from exc


@metadata_cli.command("export")
@click.option(
    "--workspace", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--revision", type=click.IntRange(min=1))
@click.option("--output", required=True, type=click.Path(path_type=Path))
def export(workspace, revision, output):
    """Export a read-only JSON projection of a stored revision."""
    try:
        snapshot = build_metadata_service(workspace).export(revision)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False, indent=2))
        click.echo(str(output))
    except (ValueError, LookupError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@metadata_cli.command("schema")
@click.option(
    "--kind",
    default="column",
    type=click.Choice(["database", "table", "column", "relation", "changes"]),
)
def schema(kind):
    """Print a type's Pydantic schema, property meanings and write permissions."""
    click.echo(json.dumps(MetadataReader.schema(kind), ensure_ascii=False, indent=2))


@metadata_cli.command("list")
@click.option(
    "--workspace", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--revision", type=click.IntRange(min=1))
@click.option(
    "--kind",
    default="table",
    type=click.Choice(["database", "table", "column", "relation"]),
)
@click.option("--table-id")
@click.option("--offset", default=0, type=click.IntRange(min=0))
@click.option("--limit", default=20, type=click.IntRange(1, 50))
def list_objects(workspace, revision, kind, table_id, offset, limit):
    """Browse the same object directory available to the Agent."""
    try:
        result = build_metadata_service(workspace).list_metadata(
            revision=revision, kind=kind, table_id=table_id, offset=offset, limit=limit
        )
        click.echo(result.model_dump_json(indent=2))
    except (ValueError, LookupError) as exc:
        raise click.ClickException(str(exc)) from exc


@metadata_cli.command("sources")
@click.option(
    "--workspace", required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--revision", type=click.IntRange(min=1))
@click.option("--source-ref", help="Read this source; omit to list source locators.")
@click.option("--offset", default=0, type=click.IntRange(min=0))
@click.option("--limit", type=click.IntRange(1, 20000))
def sources(workspace, revision, source_ref, offset, limit):
    """Inspect original source contents only when requested."""
    try:
        service = build_metadata_service(workspace)
        result = (
            service.read_source(
                source_ref, revision=revision, offset=offset, limit=limit or 12000
            )
            if source_ref
            else service.list_sources(
                revision=revision, offset=offset, limit=limit or 20
            )
        )
        click.echo(result.model_dump_json(indent=2))
    except (ValueError, LookupError) as exc:
        raise click.ClickException(str(exc)) from exc
