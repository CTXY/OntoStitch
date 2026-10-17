from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.capabilities.sql_training.builders import build_dimension_value_assets
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    build_column_table_map,
    normalize_sql_aliases,
)
from cortex.capabilities.sql_training.models import ColumnProperties, NodeAsset, TableProperties
from cortex.infrastructure.database import DatabaseConnector

_STRING_TYPE_MARKERS = ("char", "clob", "string", "text")


@dataclass(frozen=True)
class SqlColumnReference:
    source_id: str
    raw_reference: str
    usage: str
    resolved_column_ref: str | None
    data_type: str | None
    included: bool
    reason: str


@dataclass(frozen=True)
class ReferencedStringColumn:
    column_ref: str
    table_name: str
    column_name: str
    data_type: str
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class DimensionValueColumnResult:
    column_ref: str
    status: str
    non_empty_count: int
    distinct_count: int
    distinct_ratio: float
    enumeration_attempted: bool
    values_read: int
    values_emitted: int
    fingerprint: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class DimensionValueExpansionResult:
    assets: tuple[NodeAsset, ...]
    columns: tuple[DimensionValueColumnResult, ...]


def analyze_referenced_string_columns(
    records: list[dict[str, Any]],
    *,
    column_assets: list[NodeAsset],
    table_assets: list[NodeAsset],
    dialect: str,
) -> tuple[tuple[ReferencedStringColumn, ...], tuple[SqlColumnReference, ...]]:
    column_by_ref = {asset.id: asset for asset in column_assets}
    column_table_map = build_column_table_map(
        [
            {
                "table_name": _column_table_name(asset, table_assets),
                "column_name": _column_name(asset),
            }
            for asset in column_assets
        ]
    )
    sources_by_ref: dict[str, set[str]] = {}
    ledger: list[SqlColumnReference] = []
    for record in records:
        source_id = str(record.get("source_id") or "")
        sql = str(record.get("sql") or "")
        try:
            normalized = normalize_sql_aliases(
                sql,
                column_table_map=column_table_map,
                dialect=dialect,
            )
            tree = sqlglot.parse_one(normalized, dialect=dialect)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Failed to parse SQL for {source_id}: {exc}") from exc
        if tree is None:
            raise ValueError(f"Failed to parse SQL for {source_id}")
        for column in tree.find_all(expressions.Column):
            raw_reference = column.sql(dialect=dialect)
            column_ref = _resolve_column(column, column_by_ref)
            if column_ref is None:
                ledger.append(
                    SqlColumnReference(
                        source_id=source_id,
                        raw_reference=raw_reference,
                        usage=_usage(column),
                        resolved_column_ref=None,
                        data_type=None,
                        included=False,
                        reason="UNRESOLVED_OR_DERIVED",
                    )
                )
                continue
            asset = column_by_ref[column_ref]
            data = asset.properties
            if not isinstance(data, ColumnProperties):
                raise TypeError(f"Expected Column asset: {column_ref}")
            is_string = _is_string_type(data.data_type)
            ledger.append(
                SqlColumnReference(
                    source_id=source_id,
                    raw_reference=raw_reference,
                    usage=_usage(column),
                    resolved_column_ref=column_ref,
                    data_type=data.data_type,
                    included=is_string,
                    reason="STRING_COLUMN" if is_string else "NON_STRING_COLUMN",
                )
            )
            if is_string:
                sources_by_ref.setdefault(column_ref, set()).add(source_id)
    selected = tuple(
        ReferencedStringColumn(
            column_ref=column_ref,
            table_name=_column_table_name(column_by_ref[column_ref], table_assets),
            column_name=_column_name(column_by_ref[column_ref]),
            data_type=str(column_by_ref[column_ref].properties.data_type or ""),
            source_ids=tuple(sorted(source_ids)),
        )
        for column_ref, source_ids in sorted(sources_by_ref.items())
    )
    return selected, tuple(ledger)


class ReferencedDimensionValueExpander:
    def __init__(
        self,
        database: DatabaseConnector,
        *,
        max_distinct_values: int = 500,
        max_distinct_ratio: float = 0.5,
        batch_size: int = 1000,
    ):
        self.database = database
        self.max_distinct_values = max_distinct_values
        self.max_distinct_ratio = max_distinct_ratio
        self.batch_size = batch_size

    def expand(
        self, columns: tuple[ReferencedStringColumn, ...]
    ) -> DimensionValueExpansionResult:
        assets: list[NodeAsset] = []
        results: list[DimensionValueColumnResult] = []
        for column in columns:
            expanded, result = self._expand_column(column)
            assets.extend(expanded)
            results.append(result)
        return DimensionValueExpansionResult(tuple(assets), tuple(results))

    def _expand_column(
        self, column: ReferencedStringColumn
    ) -> tuple[list[NodeAsset], DimensionValueColumnResult]:
        initial = self.database.column_value_stats(column.table_name, column.column_name)
        ratio = initial.distinct_count / initial.non_empty_count if initial.non_empty_count else 0.0
        if initial.non_empty_count == 0:
            return [], _result(column, "EMPTY", initial.non_empty_count, 0, ratio)
        if (
            initial.distinct_count > self.max_distinct_values
            or ratio > self.max_distinct_ratio
        ):
            return [], _result(
                column,
                "SKIPPED_HIGH_CARDINALITY",
                initial.non_empty_count,
                initial.distinct_count,
                ratio,
                reason=_high_reason(initial.distinct_count, ratio, self),
            )

        values: dict[str, Any] = {}
        for value in self.database.iter_distinct_column_values(
            column.table_name, column.column_name, batch_size=self.batch_size
        ):
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            value_id = make_node_id(
                NodeType.DIMENSION_VALUE, column.table_name, column.column_name, value
            )
            values.setdefault(value_id, value)
            if len(values) > self.max_distinct_values:
                return [], _result(
                    column,
                    "FAILED_INCONSISTENT_READ",
                    initial.non_empty_count,
                    initial.distinct_count,
                    ratio,
                    attempted=True,
                    values_read=len(values),
                    reason="ENUMERATION_EXCEEDED_LIMIT",
                )
        final = self.database.column_value_stats(column.table_name, column.column_name)
        if final != initial or len(values) != initial.distinct_count:
            return [], _result(
                column,
                "FAILED_INCONSISTENT_READ",
                initial.non_empty_count,
                initial.distinct_count,
                ratio,
                attempted=True,
                values_read=len(values),
                reason="STATS_OR_ENUMERATION_CHANGED",
            )
        items = [
            {
                "table_name": column.table_name,
                "column_name": column.column_name,
                "value": values[value_id],
                "source_type": "database",
                "source_id": f"{column.table_name}.{column.column_name}",
            }
            for value_id in sorted(values)
        ]
        expanded = build_dimension_value_assets(items)
        fingerprint = hashlib.sha256(
            json.dumps(sorted(values), ensure_ascii=False).encode()
        ).hexdigest()
        return expanded, _result(
            column,
            "FULLY_MATERIALIZED",
            initial.non_empty_count,
            initial.distinct_count,
            ratio,
            attempted=True,
            values_read=len(values),
            values_emitted=len(expanded),
            fingerprint=fingerprint,
        )


def _resolve_column(
    column: expressions.Column, column_by_ref: dict[str, NodeAsset]
) -> str | None:
    if column.table:
        column_ref = make_node_id(NodeType.COLUMN, column.table, column.name)
        return column_ref if column_ref in column_by_ref else None
    select = column.find_ancestor(expressions.Select)
    if select is None:
        return None
    candidates = {
        make_node_id(NodeType.COLUMN, table.name, column.name)
        for table in select.find_all(expressions.Table)
        if make_node_id(NodeType.COLUMN, table.name, column.name) in column_by_ref
    }
    return candidates.pop() if len(candidates) == 1 else None


def _usage(column: expressions.Column) -> str:
    parent = column.parent
    while parent is not None:
        if isinstance(parent, expressions.Where):
            return "FILTER"
        if isinstance(parent, expressions.Having):
            return "HAVING"
        if isinstance(parent, expressions.Join):
            return "JOIN"
        if isinstance(parent, expressions.Group):
            return "GROUP_BY"
        if isinstance(parent, expressions.Order):
            return "ORDER_BY"
        if isinstance(parent, expressions.Case):
            return "CASE"
        parent = parent.parent
    return "REFERENCE"


def _result(
    column: ReferencedStringColumn,
    status: str,
    non_empty_count: int,
    distinct_count: int,
    ratio: float,
    *,
    attempted: bool = False,
    values_read: int = 0,
    values_emitted: int = 0,
    fingerprint: str | None = None,
    reason: str | None = None,
) -> DimensionValueColumnResult:
    return DimensionValueColumnResult(
        column_ref=column.column_ref,
        status=status,
        non_empty_count=non_empty_count,
        distinct_count=distinct_count,
        distinct_ratio=ratio,
        enumeration_attempted=attempted,
        values_read=values_read,
        values_emitted=values_emitted,
        fingerprint=fingerprint,
        reason=reason,
    )


def _high_reason(count: int, ratio: float, expander: ReferencedDimensionValueExpander) -> str:
    reasons = []
    if count > expander.max_distinct_values:
        reasons.append("DISTINCT_COUNT_EXCEEDED")
    if ratio > expander.max_distinct_ratio:
        reasons.append("DISTINCT_RATIO_EXCEEDED")
    return ",".join(reasons)


def _is_string_type(data_type: str | None) -> bool:
    return any(marker in str(data_type or "").casefold() for marker in _STRING_TYPE_MARKERS)


def _column_name(asset: NodeAsset) -> str:
    data = asset.properties
    if not isinstance(data, ColumnProperties):
        raise TypeError(f"Expected Column asset: {asset.id}")
    return data.column_name


def _column_table_name(asset: NodeAsset, table_assets: list[NodeAsset]) -> str:
    data = asset.properties
    if not isinstance(data, ColumnProperties):
        raise TypeError(f"Expected Column asset: {asset.id}")
    table_by_ref = {table.id: table for table in table_assets}
    table_ref = next((ref for ref in data.references if ref.startswith("table.")), None)
    table = table_by_ref.get(table_ref or "")
    if table is not None and isinstance(table.properties, TableProperties):
        return table.properties.table_name
    if table_ref:
        return table_ref.removeprefix("table.")
    raise ValueError(f"Column has no table reference: {asset.id}")
