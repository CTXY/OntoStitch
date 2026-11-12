"""Compatibility entry point for ``cortex graph-merge`` (same flags and exit codes)."""

from pathlib import Path

from cortex.cli.sql_training import graph_merge_cli


def merge(
    training_workspace: Path,
    config_path: Path,
    output: Path,
    *,
    run_id: str | None = None,
    existing_state: Path | None = None,
) -> None:
    args = [
        "--training-workspace",
        str(training_workspace),
        "--config",
        str(config_path),
        "--output",
        str(output),
    ]
    if run_id is not None:
        args.extend(["--run-id", run_id])
    if existing_state is not None:
        args.extend(["--existing-state", str(existing_state)])
    graph_merge_cli.main(args=args, standalone_mode=False)


if __name__ == "__main__":
    graph_merge_cli(prog_name="merge_training_output.py")
