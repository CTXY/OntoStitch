from __future__ import annotations

import concurrent.futures
import base64
import json
import os
from pathlib import Path
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

QueryResult = dict[str, list[str] | list[list[Any]]]


@dataclass(frozen=True)
class QueryExecutionResult:
    success: bool
    result: QueryResult | None = None
    error: str | None = None


def execute_query(
    sql: str,
    *,
    database_url: str,
    timeout: float = 30.0,
    read_only: bool = True,
    row_limit: int | None = None,
) -> QueryExecutionResult:
    worker = os.environ.get("CORTEX_SQLITE_PYTHON")
    if worker and make_url(database_url).get_backend_name() == "sqlite":
        url = make_url(database_url)
        database = url.database or ":memory:"
        if database == ":memory:":
            return QueryExecutionResult(False, error="An external SQLite worker requires a file-backed database")
        if not database.startswith("file:"):
            database = Path(database).resolve().as_uri()
        from urllib.parse import urlencode
        parameters = {k: v for k, v in url.query.items() if k != "uri"}
        if read_only:
            parameters["mode"] = "ro"
        database += ("&" if "?" in database else "?") + urlencode(parameters)
        request = {"database": database, "sql": sql, "timeout": timeout,
                   "read_only": read_only, "row_limit": row_limit}
        try:
            result = subprocess.run([worker, str(Path(__file__).with_name("sqlite_worker.py"))],
                input=json.dumps(request), text=True, capture_output=True, timeout=timeout + 2, check=True)
            def decode(value):
                if set(value) == {"__sqlite_blob__"}:
                    return base64.b64decode(value["__sqlite_blob__"])
                return value
            packet = json.loads(result.stdout, object_hook=decode)
            return QueryExecutionResult(**packet["execution"])
        except subprocess.TimeoutExpired:
            return QueryExecutionResult(False, error=f"query execution timed out after {timeout} seconds")
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError) as error:
            return QueryExecutionResult(False, error=f"SQLite worker unavailable: {error}")
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        _execute_query, sql, database_url, timeout, read_only, row_limit
    )
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return QueryExecutionResult(
            success=False,
            error=f"query execution timed out after {timeout} seconds",
        )
    except Exception as exc:
        return QueryExecutionResult(success=False, error=str(exc))
    finally:
        # Waiting here would defeat the public timeout when a driver cannot cancel an active query.
        executor.shutdown(wait=False, cancel_futures=True)


def _execute_query(
    sql: str,
    database_url: str,
    timeout: float,
    read_only: bool = False,
    row_limit: int | None = None,
) -> QueryExecutionResult:
    deadline = time.monotonic() + timeout
    engine = create_engine(database_url)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def set_sqlite_deadline(dbapi_connection: Any, _connection_record: Any) -> None:
            if read_only:
                dbapi_connection.execute("PRAGMA query_only=ON")
            dbapi_connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline),
                1_000,
            )

    try:
        with engine.connect() as connection:
            if read_only and engine.dialect.name == "mysql":
                connection.exec_driver_sql("START TRANSACTION READ ONLY")
            result = connection.exec_driver_sql(
                _prepare_driver_sql(sql, engine.dialect.name)
            )
            columns = list(result.keys())
            values = (
                result.fetchmany(row_limit)
                if row_limit is not None
                else result.fetchall()
            )
            rows: list[list[Any]] = [list(row) for row in values]
        return QueryExecutionResult(
            success=True,
            result={"columns": columns, "rows": rows},
        )
    except Exception as exc:
        if time.monotonic() >= deadline:
            return QueryExecutionResult(
                success=False,
                error=f"query execution timed out after {timeout} seconds",
            )
        return QueryExecutionResult(success=False, error=str(exc))
    finally:
        engine.dispose()


def _prepare_driver_sql(sql: str, dialect: str) -> str:
    if dialect == "mysql":
        return sql.replace("%", "%%")
    return sql
