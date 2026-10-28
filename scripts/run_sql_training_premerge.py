"""Compatibility entry point for ``cortex sql-extract`` (same flags and exit codes)."""

from cortex.cli.sql_training import sql_extraction_cli


def main() -> None:
    sql_extraction_cli(prog_name="run_sql_training_premerge.py")


if __name__ == "__main__":
    main()
