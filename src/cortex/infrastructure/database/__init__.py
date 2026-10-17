from typing import Any, Protocol

from cortex.infrastructure.database.base import DatabaseConnector
from cortex.infrastructure.database.execution import QueryExecutionResult, execute_query
from cortex.infrastructure.database.models import (
    CatalogSnapshot,
    ColumnProfile,
    ColumnValueStats,
    JoinProfile,
    TableProfile,
)
from cortex.infrastructure.database.sqlalchemy import SQLAlchemyDatabase


class DatabaseClient(Protocol):
    def execute(self, sql: str, *, timeout: float | None = None) -> list[dict[str, Any]]: ...


__all__ = [
    "DatabaseClient",
    "DatabaseConnector",
    "CatalogSnapshot",
    "ColumnProfile",
    "ColumnValueStats",
    "JoinProfile",
    "QueryExecutionResult",
    "SQLAlchemyDatabase",
    "TableProfile",
    "execute_query",
]
