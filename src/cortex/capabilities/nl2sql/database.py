"""Bounded, read-only SQLite tools for the interactive agent."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import sqlglot
from sqlglot import exp


def validate_sql(sql: str) -> dict:
    statements = sqlglot.parse(sql, read="sqlite")
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
    )
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise ValueError("Query contains a non-read-only operation")
    # Canonical text is the exact version shown, grounded and executed.
    canonical = tree.sql(dialect="sqlite", pretty=True)
    return {
        "sql": canonical,
        "sql_hash": hashlib.sha256(canonical.encode()).hexdigest(),
    }


class QueryDatabase:
    def __init__(self, path: Path, *, timeout: float = 8, max_rows: int = 200):
        self.path, self.timeout, self.max_rows = path.resolve(), timeout, max_rows

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + self.timeout
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        # Defense in depth, including extension/file functions and writes in CTEs.
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_RECURSIVE,
        }

        def authorize(action, first, second, *_):
            if action == sqlite3.SQLITE_FUNCTION and str(second or first).lower() in {
                "load_extension",
                "writefile",
                "readfile",
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        try:
            yield db, authorize
        finally:
            db.close()

    @staticmethod
    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    def schema(self, tables: list[str] | None = None):
        with self.connect() as (db, _):
            names = [
                r[0]
                for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            if tables and not set(tables) <= set(names):
                raise ValueError("Unknown table")
            return [
                {
                    "name": name,
                    "columns": [
                        {
                            "name": r[1],
                            "type": r[2],
                            "primary_key": bool(r[5]),
                            "nullable": not bool(r[3]) and not bool(r[5]),
                        }
                        for r in db.execute(f"PRAGMA table_info({self.quote(name)})")
                    ],
                    "foreign_keys": [
                        {"column": r[3], "table": r[2], "target": r[4]}
                        for r in db.execute(
                            f"PRAGMA foreign_key_list({self.quote(name)})"
                        )
                    ],
                }
                for name in names
                if not tables or name in tables
            ]

    def values(self, table: str, column: str, limit: int = 8):
        schema = self.schema([table])[0]
        if column not in {c["name"] for c in schema["columns"]}:
            raise ValueError("Unknown column")
        return self.execute(
            f"SELECT DISTINCT {self.quote(column)} FROM {self.quote(table)} LIMIT {min(max(int(limit), 1), 20)}"
        )

    def validate(self, sql: str):
        result = validate_sql(sql)
        with self.connect() as (db, authorizer):
            db.set_authorizer(authorizer)
            db.execute("EXPLAIN QUERY PLAN " + result["sql"]).fetchmany(200)
        return result

    def execute(self, sql: str):
        validated = validate_sql(sql)
        started = time.monotonic()
        with self.connect() as (db, authorizer):
            db.set_authorizer(authorizer)
            cursor = db.execute(validated["sql"])
            rows = cursor.fetchmany(self.max_rows + 1)

            def cell(value):
                if isinstance(value, bytes):
                    return f"[BLOB: {len(value)} bytes]"
                return value[:4000] if isinstance(value, str) else value

            return {
                "columns": [c[0] for c in cursor.description],
                "rows": [[cell(v) for v in row] for row in rows[: self.max_rows]],
                "truncated": len(rows) > self.max_rows,
                "row_count": min(len(rows), self.max_rows),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
