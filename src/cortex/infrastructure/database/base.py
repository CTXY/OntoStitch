from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from cortex.infrastructure.database.models import CatalogSnapshot, ColumnValueStats


class DatabaseConnector(Protocol):
    def introspect_catalog(self, *, include_tables: list[str] | None = None, sample_values: bool = True) -> CatalogSnapshot: ...

    def distinct_column_values(self, table_name: str, column_name: str) -> list[Any]: ...

    def column_value_stats(self, table_name: str, column_name: str) -> ColumnValueStats: ...

    def iter_distinct_column_values(
        self,
        table_name: str,
        column_name: str,
        *,
        batch_size: int = 1000,
    ) -> Iterator[Any]: ...

    def find_exact_column_values(
        self, table_name: str, column_name: str, value: Any
    ) -> list[Any]: ...

    def sample_rows(self, table_name: str, limit: int = 5) -> list[dict[str, object]]: ...

    def run_validation_query(self, sql: str) -> list[dict[str, object]]: ...

    def get_dialect(self) -> str: ...
