from __future__ import annotations

import json
from pathlib import Path

import click

from nl2sql_baseline.config import load_settings
from nl2sql_baseline.workflow import Workflow


@click.group()
def main() -> None:
    """Run the standalone schema-linking and prompting NL2SQL baseline."""


def _common_options(function):  # type: ignore[no-untyped-def]
    function = click.option(
        "--env-file",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        help="Environment file. Defaults to the repository root .env.",
    )(function)
    return click.option(
        "--config",
        "config_path",
        required=True,
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )(function)


@main.command("preflight")
@_common_options
@click.option(
    "--skip-gold-execution",
    is_flag=True,
    help="Check connectivity and schema without executing gold SQL.",
)
def preflight(config_path: Path, env_file: Path | None, skip_gold_execution: bool) -> None:
    """Validate inputs, external database schema, gold SQL, and configuration."""
    try:
        result = Workflow(load_settings(config_path, env_file=env_file)).preflight(
            execute_gold=not skip_gold_execution
        )
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, ensure_ascii=False))


@main.command("evaluate")
@_common_options
@click.option(
    "--output",
    type=click.Path(file_okay=False, path_type=Path),
    help="Override the configured output workspace root.",
)
def evaluate(config_path: Path, env_file: Path | None, output: Path | None) -> None:
    """Link schema, optionally retrieve examples, generate SQL, and report EA."""
    try:
        run, report = Workflow(load_settings(config_path, env_file=env_file)).evaluate(output)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps({"output": str(run), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()
