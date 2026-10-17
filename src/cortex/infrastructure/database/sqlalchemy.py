from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.sql import text

from cortex.infrastructure.database.execution import _prepare_driver_sql
from cortex.infrastructure.database.models import (
    CatalogSnapshot,
    ColumnProfile,
    ColumnValueStats,
    JoinProfile,
    TableProfile,
)

_SAMPLE_TEXT_LIMIT = 80


class SQLAlchemyDatabase:
    def __init__(self, url: str, *, engine: Engine | None = None):
        self.url = url
        self.engine = engine or create_engine(url)
        self._table_names_cache: set[str] | None = None
        self._columns_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def introspect_catalog(self, *, include_tables: list[str] | None = None) -> CatalogSnapshot:
        inspector = inspect(self.engine)
        include = set(include_tables or [])
        table_names = [
            table_name
            for table_name in inspector.get_table_names()
            if not include or table_name in include
        ]
        tables: list[TableProfile] = []
        columns: list[ColumnProfile] = []
        joins: list[JoinProfile] = []
        for table_name in table_names:
            pk_columns = set(
                inspector.get_pk_constraint(table_name).get("constrained_columns") or []
            )
            foreign_keys = inspector.get_foreign_keys(table_name)
            fk_columns = {
                column for fk in foreign_keys for column in fk.get("constrained_columns") or []
            }
            for fk in foreign_keys:
                referred_table = fk.get("referred_table")
                if not referred_table or (include and referred_table not in include):
                    continue
                left_columns = fk.get("constrained_columns") or []
                right_columns = fk.get("referred_columns") or []
                joins.append(
                    JoinProfile(
                        from_table=table_name,
                        to_table=referred_table,
                        column_pairs=list(zip(left_columns, right_columns, strict=False)),
                        constraint_name=fk.get("name"),
                    )
                )
            tables.append(
                TableProfile(
                    name=table_name,
                    comment=_table_comment(inspector, table_name),
                    primary_keys=sorted(pk_columns),
                    foreign_keys=sorted(fk_columns),
                )
            )
            for column in inspector.get_columns(table_name):
                self._cache_column_info(table_name, column)
                columns.append(
                    ColumnProfile(
                        table_name=table_name,
                        column_name=column["name"],
                        data_type=str(column.get("type") or ""),
                        nullable=column.get("nullable"),
                        default=(
                            str(column["default"]) if column.get("default") is not None else None
                        ),
                        comment=column.get("comment"),
                        sample_values=self.sample_column_values(
                            table_name, column["name"], column=column
                        ),
                        primary_key=column["name"] in pk_columns,
                        foreign_key=column["name"] in fk_columns,
                    )
                )
        return CatalogSnapshot(tables=tables, columns=columns, joins=joins)

    def distinct_column_values(self, table_name: str, column_name: str) -> list[Any]:
        return sorted(
            self.iter_distinct_column_values(table_name, column_name),
            key=lambda value: str(value),
        )

    def column_value_stats(self, table_name: str, column_name: str) -> ColumnValueStats:
        column = self._column_info(table_name, column_name)
        where = self._non_empty_where_clause(column_name, column)
        quoted_table = self._quote(table_name)
        quoted_column = self._quote(column_name)
        query = text(
            f"select count(*) as non_empty_count, "
            f"count(distinct {quoted_column}) as distinct_count "
            f"from {quoted_table} where {where}"
        )
        with self.engine.connect() as connection:
            row = connection.execute(query).mappings().one()
        return ColumnValueStats(
            non_empty_count=int(row["non_empty_count"] or 0),
            distinct_count=int(row["distinct_count"] or 0),
        )

    def iter_distinct_column_values(
        self,
        table_name: str,
        column_name: str,
        *,
        batch_size: int = 1000,
    ) -> Iterator[Any]:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        column = self._column_info(table_name, column_name)
        where = self._non_empty_where_clause(column_name, column)
        quoted_table = self._quote(table_name)
        quoted_column = self._quote(column_name)
        query = text(f"select distinct {quoted_column} as value from {quoted_table} where {where}")
        with self.engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(query)
            while rows := result.fetchmany(batch_size):
                yield from (row[0] for row in rows)

    def find_exact_column_values(
        self, table_name: str, column_name: str, value: Any
    ) -> list[Any]:
        self._column_info(table_name, column_name)
        quoted_table = self._quote(table_name)
        quoted_column = self._quote(column_name)
        query = text(
            f"select distinct {quoted_column} as value from {quoted_table} "
            f"where {quoted_column} = :value limit 2"
        )
        with self.engine.connect() as connection:
            return [row[0] for row in connection.execute(query, {"value": value})]

    def sample_rows(self, table_name: str, limit: int = 5) -> list[dict[str, object]]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if table_name not in set(inspect(self.engine).get_table_names()):
            raise ValueError(f"Table not found: {table_name}")
        query = text(f"select * from {self._quote(table_name)} limit {int(limit)}")
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(query).mappings().all()]

    def run_validation_query(self, sql: str) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            driver_sql = _prepare_driver_sql(sql, self.engine.dialect.name)
            return [dict(row) for row in connection.exec_driver_sql(driver_sql).mappings().all()]

    def get_dialect(self) -> str:
        return self.engine.dialect.name

    def sample_column_values(
        self,
        table_name: str,
        column_name: str,
        *,
        limit: int = 3,
        column: dict[str, Any] | None = None,
    ) -> list[Any]:
        if limit <= 0:
            return []
        column = column or self._column_info(table_name, column_name)
        where = self._non_empty_where_clause(column_name, column)
        quoted_table = self._quote(table_name)
        quoted_column = self._quote(column_name)
        query = text(
            f"select distinct {quoted_column} as value "
            f"from {quoted_table} "
            f"where {where} "
            f"order by {_random_function(self.engine.dialect.name)} "
            f"limit {int(limit)}"
        )
        with self.engine.connect() as connection:
            values = [row[0] for row in connection.execute(query).fetchall()]
        return [_truncate_sample_value(value) for value in values]

    def _column_info(self, table_name: str, column_name: str) -> dict[str, Any]:
        inspector = inspect(self.engine)
        table_names = self._table_names(inspector)
        if table_name not in table_names:
            raise ValueError(f"Table not found: {table_name}")
        columns = self._columns_for_table(inspector, table_name)
        if column_name not in columns:
            raise ValueError(f"Column not found: {table_name}.{column_name}")
        return columns[column_name]

    def _table_names(self, inspector: Any) -> set[str]:
        if self._table_names_cache is None:
            self._table_names_cache = set(inspector.get_table_names())
        return self._table_names_cache

    def _columns_for_table(self, inspector: Any, table_name: str) -> dict[str, dict[str, Any]]:
        cached = self._columns_cache.get(table_name)
        if cached is not None:
            return cached
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        self._columns_cache[table_name] = columns
        return columns

    def _cache_column_info(self, table_name: str, column: dict[str, Any]) -> None:
        self._columns_cache.setdefault(table_name, {})[column["name"]] = column

    def _quote(self, identifier: str) -> str:
        return self.engine.dialect.identifier_preparer.quote(identifier)

    def _non_empty_where_clause(self, column_name: str, column: dict[str, Any]) -> str:
        quoted_column = self._quote(column_name)
        where = f"{quoted_column} is not null"
        if _is_text_column(column):
            where += f" and trim({quoted_column}) <> ''"
        return where


def _table_comment(inspector: Any, table_name: str) -> str | None:
    try:
        comment = inspector.get_table_comment(table_name).get("text")
    except Exception:  # noqa: BLE001 - dialects may not support table comments.
        return None
    return str(comment) if comment else None


def _is_text_column(column: dict[str, Any]) -> bool:
    data_type = str(column.get("type") or "").lower()
    return any(token in data_type for token in ("char", "text", "string", "varchar"))


def _random_function(dialect_name: str) -> str:
    if dialect_name in {"mysql", "mariadb"}:
        return "rand()"
    return "random()"


def _truncate_sample_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _SAMPLE_TEXT_LIMIT:
        return value[:_SAMPLE_TEXT_LIMIT]
    return value
