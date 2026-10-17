from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url

from cortex.capabilities.sql_training.runtime_config import RuntimeDatabaseConfig


def prepare_sqlite_database(
    *,
    output: str | Path,
    schema_path: str | Path,
    seed_path: str | Path | None = None,
    overwrite: bool = False,
) -> Path:
    output_path = Path(output)
    if output_path.exists() and not overwrite:
        raise FileExistsError(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    schema_sql = Path(schema_path).read_text(encoding="utf-8")
    seed_sql = Path(seed_path).read_text(encoding="utf-8") if seed_path else ""

    connection = sqlite3.connect(output_path)
    try:
        connection.executescript(schema_sql)
        if seed_sql:
            connection.executescript(seed_sql)
        connection.commit()
    finally:
        connection.close()
    return output_path


def ensure_sqlite_database_ready(config: RuntimeDatabaseConfig) -> Path | None:
    if not _uses_sqlite(config):
        return None
    output_path = _sqlite_database_path(config.url)
    if output_path.exists() and not config.sqlite_overwrite:
        return output_path
    if not config.sqlite_schema_path:
        raise ValueError(
            "database.sqlite_schema_path is required when SQLite database file is missing "
            "or database.sqlite_overwrite is true"
        )
    return prepare_sqlite_database(
        output=output_path,
        schema_path=config.sqlite_schema_path,
        seed_path=config.sqlite_seed_path,
        overwrite=config.sqlite_overwrite,
    )


def _uses_sqlite(config: RuntimeDatabaseConfig) -> bool:
    if (config.dialect or "").lower() == "sqlite":
        return True
    return bool(config.url and config.url.startswith("sqlite"))


def _sqlite_database_path(database_url: str | None) -> Path:
    if not database_url:
        raise ValueError("database.url is required for SQLite bootstrap")
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        raise ValueError("database.dialect=sqlite requires a sqlite database.url")
    database = url.database
    if not database or database == ":memory:":
        raise ValueError("SQLite bootstrap requires a file database URL, not sqlite:///:memory:")
    return Path(database)
