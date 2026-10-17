from __future__ import annotations

import csv
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from nl2sql_baseline.models import QueryResult


@dataclass(frozen=True)
class SchemaSnapshot:
    ddl: str
    catalog: dict[str, Any]


class Database:
    def __init__(
        self,
        url: str,
        *,
        engine: Engine | None = None,
        query_timeout: float | None = None,
    ):
        self.engine = engine or create_engine(url, pool_pre_ping=True)
        self.query_timeout = query_timeout

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def introspect_schema(
        self,
        include_tables: tuple[str, ...] = (),
        *,
        description_dir: Path | None = None,
    ) -> SchemaSnapshot:
        inspector = inspect(self.engine)
        available = set(inspector.get_table_names())
        if not include_tables:
            include_tables = tuple(sorted(available))
        missing = sorted(set(include_tables) - available)
        if missing:
            raise ValueError(f"configured database tables do not exist: {missing}")
        descriptions = _load_descriptions(description_dir)
        catalog: dict[str, Any] = {"dialect": self.engine.dialect.name, "tables": []}
        ddl_parts: list[str] = []
        quote = self.engine.dialect.identifier_preparer.quote
        for table_name in include_tables:
            columns = inspector.get_columns(table_name)
            pk = set(inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
            foreign_keys = inspector.get_foreign_keys(table_name)
            table_payload = {"name": table_name, "columns": [], "foreign_keys": []}
            for foreign_key in foreign_keys:
                referred_table = foreign_key.get("referred_table")
                constrained = foreign_key.get("constrained_columns") or []
                referred = foreign_key.get("referred_columns") or []
                for source_column, target_column in zip(constrained, referred, strict=False):
                    table_payload["foreign_keys"].append(
                        {
                            "column": str(source_column),
                            "referred_table": str(referred_table),
                            "referred_column": str(target_column),
                        }
                    )
            definitions = []
            for column in columns:
                name = str(column["name"])
                description = descriptions.get((table_name.casefold(), name.casefold()), {})
                payload = {
                    "name": name,
                    "type": str(column.get("type") or ""),
                    "nullable": bool(column.get("nullable", True)),
                    "default": str(column["default"])
                    if column.get("default") is not None
                    else None,
                    "comment": column.get("comment"),
                    "primary_key": name in pk,
                    "description": description.get("description", ""),
                    "expanded_name": description.get("expanded_name", ""),
                    "value_description": description.get("value_description", ""),
                }
                table_payload["columns"].append(payload)
                definition = f"  {quote(name)} {payload['type']}"
                if not payload["nullable"]:
                    definition += " NOT NULL"
                if payload["comment"]:
                    comment = str(payload["comment"]).replace("'", "''")
                    definition += f" COMMENT '{comment}'"
                definitions.append(definition)
            if pk:
                definitions.append(
                    "  PRIMARY KEY (" + ", ".join(quote(item) for item in sorted(pk)) + ")"
                )
            for foreign_key in table_payload["foreign_keys"]:
                definitions.append(
                    "  FOREIGN KEY ("
                    + quote(foreign_key["column"])
                    + ") REFERENCES "
                    + quote(foreign_key["referred_table"])
                    + " ("
                    + quote(foreign_key["referred_column"])
                    + ")"
                )
            ddl_parts.append(
                f"CREATE TABLE {quote(table_name)} (\n" + ",\n".join(definitions) + "\n);"
            )
            catalog["tables"].append(table_payload)
        return SchemaSnapshot(ddl="\n\n".join(ddl_parts), catalog=catalog)

    def sample_distinct_values(self, table: str, column: str, *, limit: int) -> list[str]:
        if limit < 1:
            return []
        quote = self.engine.dialect.identifier_preparer.quote
        statement = text(
            f"SELECT DISTINCT {quote(column)} FROM {quote(table)} "
            f"WHERE {quote(column)} IS NOT NULL LIMIT {int(limit)}"
        )
        with self.engine.connect() as connection:
            deadline, raw_connection = self._install_sqlite_timeout(connection)
            try:
                return [
                    str(row[0])
                    for row in connection.execute(statement)
                    if row[0] is not None and str(row[0]).strip()
                ]
            except Exception as exc:
                self._raise_timeout_if_interrupted(exc, deadline)
                raise
            finally:
                self._clear_sqlite_timeout(raw_connection)

    def execute_read_only(self, sql: str) -> QueryResult:
        _ensure_read_only(sql)
        statement = sql
        if self.engine.dialect.name == "mysql":
            statement = statement.replace("%", "%%")
        with self.engine.connect() as connection:
            deadline, raw_connection = self._install_sqlite_timeout(connection)
            try:
                result = connection.exec_driver_sql(statement)
                rows = [tuple(row) for row in result.fetchall()]
                return QueryResult(columns=[str(item) for item in result.keys()], rows=rows)
            except Exception as exc:
                self._raise_timeout_if_interrupted(exc, deadline)
                raise
            finally:
                self._clear_sqlite_timeout(raw_connection)

    def _install_sqlite_timeout(self, connection: Any) -> tuple[float | None, Any | None]:
        if self.engine.dialect.name != "sqlite" or not self.query_timeout:
            return None, None
        deadline = time.monotonic() + self.query_timeout
        raw_connection = connection.connection.driver_connection

        def _abort_when_expired() -> int:
            return int(time.monotonic() > deadline)

        raw_connection.set_progress_handler(_abort_when_expired, 1000)
        return deadline, raw_connection

    def _clear_sqlite_timeout(self, raw_connection: Any | None) -> None:
        if raw_connection is not None:
            raw_connection.set_progress_handler(None, 0)

    def _raise_timeout_if_interrupted(self, exc: Exception, deadline: float | None) -> None:
        if deadline is not None and "interrupted" in str(exc).lower():
            raise TimeoutError(f"SQL execution exceeded {self.query_timeout:g}s") from exc


def _ensure_read_only(sql: str) -> None:
    normalized = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.DOTALL).strip().lower()
    if ";" in normalized.rstrip(";") or not normalized.startswith(("select", "with")):
        raise ValueError("SQL must be one read-only SELECT or WITH query")
    if re.search(
        r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|call)\b", normalized
    ):
        raise ValueError("SQL contains a non-read-only statement")


def _load_descriptions(description_dir: Path | None) -> dict[tuple[str, str], dict[str, str]]:
    if description_dir is None or not description_dir.exists():
        return {}
    result: dict[tuple[str, str], dict[str, str]] = {}
    for path in sorted(description_dir.glob("*.csv")):
        rows = _read_csv(path)
        for row in rows:
            column = str(row.get("original_column_name") or "").strip()
            if not column:
                continue
            result[(path.stem.casefold(), column.casefold())] = {
                "expanded_name": str(row.get("column_name") or "").strip(),
                "description": str(row.get("column_description") or "").strip(),
                "value_description": str(row.get("value_description") or "").strip(),
            }
    return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with path.open(encoding=encoding, newline="") as handle:
                return list(csv.DictReader(handle))
        except UnicodeDecodeError:
            continue
    return []
