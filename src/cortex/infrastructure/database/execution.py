from __future__ import annotations

import concurrent.futures
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, event

QueryResult = dict[str, list[str] | list[list[Any]]]


@dataclass(frozen=True)
class QueryExecutionResult:
    success: bool
    result: QueryResult | None = None
    error: str | None = None


def execute_query(sql: str, *, database_url: str, timeout: float = 30.0) -> QueryExecutionResult:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_execute_query, sql, database_url, timeout)
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


def _execute_query(sql: str, database_url: str, timeout: float) -> QueryExecutionResult:
    deadline = time.monotonic() + timeout
    engine = create_engine(database_url)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def set_sqlite_deadline(dbapi_connection: Any, _connection_record: Any) -> None:
            dbapi_connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline),
                1_000,
            )

    try:
        with engine.connect() as connection:
            result = connection.exec_driver_sql(_prepare_driver_sql(sql, engine.dialect.name))
            columns = list(result.keys())
            rows: list[list[Any]] = [list(row) for row in result.fetchall()]
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
