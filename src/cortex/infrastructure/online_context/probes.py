"""Application-bound read-only probes; no model-selected database connections."""

from __future__ import annotations

import hashlib
from pathlib import Path

import sqlglot
from sqlglot import exp

from cortex.infrastructure.database.execution import execute_query
from cortex.infrastructure.database.read_only_sqlite import QueryDatabase
from cortex.infrastructure.database.sqlalchemy import SQLAlchemyDatabase


class SQLiteFilterDatabase:
    dialect = "sqlite"

    def __init__(self, path: Path, *, timeout: float = 8, max_rows: int = 200):
        self.timeout = timeout
        self.database = QueryDatabase(path, timeout=timeout, max_rows=max_rows)
        stat = self.database.path.stat()
        self.context_fingerprint = {
            "dialect": self.dialect,
            "path": str(self.database.path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "timeout": timeout,
            "max_rows": max_rows,
        }

    def schema(self, tables: list[str]):
        return {"tables": self.database.schema(tables), "truncated": False}

    def execute(self, sql: str):
        return self.database.execute(sql)


class MySQLFilterDatabase:
    dialect = "mysql"

    def __init__(self, url: str, *, timeout: float = 8, max_rows: int = 200):
        self._url, self.timeout, self.max_rows = url, timeout, max_rows
        self.context_fingerprint = {
            "dialect": self.dialect,
            "connection_sha256": hashlib.sha256(url.encode()).hexdigest(),
            "timeout": timeout,
            "max_rows": max_rows,
        }

    def schema(self, tables: list[str]):
        db = SQLAlchemyDatabase(self._url)
        try:
            return {
                "catalog": db.introspect_catalog(include_tables=tables).model_dump(
                    mode="json"
                ),
                "truncated": False,
            }
        finally:
            db.engine.dispose()

    def execute(self, sql: str):
        try:
            statements = sqlglot.parse(sql, read=self.dialect)
        except sqlglot.errors.SqlglotError as exc:
            raise ValueError(f"Invalid probe SQL: {exc}") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError("Only a single read-only SELECT query is allowed")
        tree = statements[0]
        forbidden = (
            exp.Insert,
            exp.Update,
            exp.Delete,
            exp.Create,
            exp.Drop,
            exp.Command,
            exp.Into,
            exp.Lock,
        )
        if any(isinstance(node, forbidden) for node in tree.walk()):
            raise ValueError("Query contains a non-read-only operation")
        if any(
            isinstance(node, exp.Func)
            and node.name.casefold() in {"load_file", "sleep", "benchmark", "get_lock"}
            for node in tree.walk()
        ):
            raise ValueError("Query contains an unsupported function")
        canonical = tree.sql(dialect=self.dialect)
        result = execute_query(
            canonical,
            database_url=self._url,
            timeout=self.timeout,
            read_only=True,
            row_limit=self.max_rows + 1,
        )
        if not result.success:
            raise ValueError(
                (result.error or "query failed").replace(self._url, "[database]")
            )
        payload = result.result or {"columns": [], "rows": []}
        rows = payload["rows"]
        values_truncated = False
        clean = []
        for row in rows[: self.max_rows]:
            cells = []
            for value in row:
                if isinstance(value, bytes) or (
                    isinstance(value, str) and len(value) > 4000
                ):
                    values_truncated = True
                    value = "[BLOB]" if isinstance(value, bytes) else value[:4000]
                cells.append(value)
            clean.append(cells)
        return {
            "columns": payload["columns"],
            "rows": clean,
            "row_count": len(clean),
            "truncated": len(rows) > self.max_rows,
            "values_truncated": values_truncated,
        }
