from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cortex.capabilities.sql_training.core.enums import (
    KeyRole,
    NodeType,
    SemanticRole,
    SourceType,
    UnitType,
)
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.models import (
    ColumnProperties,
    JoinCondition,
    NodeAsset,
    Source,
    TableProperties,
)
from cortex.infrastructure.database import ColumnProfile, JoinProfile, TableProfile
from cortex.schema import GraphEdge, SourceReference

_SAMPLE_TEXT_LIMIT = 80
_SAMPLE_VALUE_LIMIT = 3


@dataclass(frozen=True)
class CatalogAssets:
    tables: list[NodeAsset]
    columns: list[NodeAsset]
    join_edges: list[GraphEdge]


def build_catalog_assets(
    tables: list[TableProfile],
    columns: list[ColumnProfile],
    joins: list[JoinProfile] | None = None,
) -> CatalogAssets:
    columns_by_table: dict[str, list[ColumnProfile]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_name, []).append(column)

    declared_joins = list(joins or [])
    inferred_joins = _shared_column_joins(tables, columns, existing=declared_joins)
    all_joins = declared_joins + inferred_joins
    table_assets = [
        _table_asset(
            table,
            columns_by_table.get(table.table_name, []),
            declared_joins=declared_joins,
        )
        for table in tables
    ]
    column_assets = [_column_asset(column) for column in columns]
    join_edges = [_join_edge(join) for join in all_joins]
    return CatalogAssets(tables=table_assets, columns=column_assets, join_edges=join_edges)


def _table_asset(
    table: TableProfile,
    columns: list[ColumnProfile],
    *,
    declared_joins: list[JoinProfile],
) -> NodeAsset:
    table_schema = _render_table_schema(table, columns, declared_joins)
    return NodeAsset(
        id=make_node_id(NodeType.TABLE, table.table_name),
        node_type=NodeType.TABLE,
        name=table.comment or table.table_name,
        aliases=[],
        description=None,
        properties=TableProperties(
            database=table.database,
            database_description=table.comment,
            table_name=table.table_name,
            table_schema=table_schema,
            primary_keys=table.primary_keys,
            foreign_keys=table.foreign_keys,
            important_columns=[],
            available_time_columns=_time_columns(columns),
            default_time_column=None,
        ),
        sources=[Source(source_type=SourceType.DATABASE, source_id=table.table_name)],
    )


def _render_table_schema(
    table: TableProfile,
    columns: list[ColumnProfile],
    declared_joins: list[JoinProfile],
) -> str:
    pk_columns = set(table.primary_keys)
    fk_targets = _fk_targets_by_column(table.table_name, declared_joins)
    lines = [_render_column_schema_line(column, pk_columns, fk_targets) for column in columns]

    relationship_lines = _foreign_key_relationship_lines(table.table_name, declared_joins)
    if relationship_lines:
        lines.extend(["", "# Foreign key relationships"])
        lines.extend(f"- {line}" for line in relationship_lines)
    return "\n".join(lines)


def _render_column_schema_line(
    column: ColumnProfile,
    pk_columns: set[str],
    fk_targets: dict[str, list[str]],
) -> str:
    tags: list[str] = []
    if column.primary_key or column.column_name in pk_columns:
        tags.append("PK")
    tags.extend(f"FK -> {target}" for target in fk_targets.get(column.column_name, []))
    tag_suffix = f" [{' ; '.join(tags)}]" if tags else ""
    description = " ".join(part for part in (column.data_type or "", column.comment or "") if part)
    if description:
        return f"- {column.column_name}: {description}{tag_suffix}"
    return f"- {column.column_name}:{tag_suffix}"


def _fk_targets_by_column(
    table_name: str,
    declared_joins: list[JoinProfile],
) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    for join in declared_joins:
        if join.from_table != table_name:
            continue
        for left_column, right_column in join.column_pairs:
            targets.setdefault(left_column, []).append(f"{join.to_table}.{right_column}")
    return {column: list(dict.fromkeys(values)) for column, values in targets.items()}


def _foreign_key_relationship_lines(
    table_name: str,
    declared_joins: list[JoinProfile],
) -> list[str]:
    lines: list[str] = []
    for join in declared_joins:
        if join.from_table != table_name and join.to_table != table_name:
            continue
        for left_column, right_column in join.column_pairs:
            lines.append(f"{join.from_table}.{left_column} = {join.to_table}.{right_column}")
    return list(dict.fromkeys(lines))


def _column_asset(column: ColumnProfile) -> NodeAsset:
    table_ref = make_node_id(NodeType.TABLE, column.table_name)
    return NodeAsset(
        id=make_node_id(NodeType.COLUMN, column.table_name, column.column_name),
        node_type=NodeType.COLUMN,
        name=column.comment or column.column_name,
        aliases=[],
        description=column.comment,
        properties=ColumnProperties(
            references=[table_ref],
            column_name=column.column_name,
            data_type=column.data_type,
            sample_values=_sample_values(column),
            nullable=column.nullable,
            semantic_role=_infer_semantic_role(column),
            key_role=_infer_key_role(column),
            unit=_infer_unit(column),
        ),
        sources=[
            Source(
                source_type=SourceType.DATABASE,
                source_id=f"{column.table_name}.{column.column_name}",
            )
        ],
    )


def _join_edge(join: JoinProfile) -> GraphEdge:
    from_table_ref = make_node_id(NodeType.TABLE, join.from_table)
    to_table_ref = make_node_id(NodeType.TABLE, join.to_table)
    join_name = f"{join.from_table}.{join.to_table}"
    edge_key = f"joins_with:{join_name}:{join.column_pairs!r}"
    edge_id = f"edge_{hashlib.sha256(edge_key.encode()).hexdigest()[:24]}"
    return GraphEdge(
        id=edge_id,
        edge_type="joins_with",
        from_node_id=from_table_ref,
        to_node_id=to_table_ref,
        properties={
            "default_join_type": "LEFT",
            "conditions": [
                JoinCondition(
                    left_column_ref=make_node_id(NodeType.COLUMN, join.from_table, left),
                    right_column_ref=make_node_id(NodeType.COLUMN, join.to_table, right),
                    operator="=",
                ).model_dump(mode="json")
                for left, right in join.column_pairs
            ],
        },
        sources=(
            SourceReference(
                source_id=join.constraint_name or join_name,
                locator=SourceType.DATABASE.value,
            ),
        ),
    )


def _shared_column_joins(
    tables: list[TableProfile],
    columns: list[ColumnProfile],
    *,
    existing: list[JoinProfile],
) -> list[JoinProfile]:
    existing_pairs = {(join.from_table, join.to_table) for join in existing}
    columns_by_table: dict[str, set[str]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_name, set()).add(column.column_name)

    inferred: list[JoinProfile] = []
    sorted_tables = sorted(table.table_name for table in tables)
    for left_index, left_table in enumerate(sorted_tables):
        for right_table in sorted_tables[left_index + 1 :]:
            if (left_table, right_table) in existing_pairs or (
                right_table,
                left_table,
            ) in existing_pairs:
                continue
            shared = sorted(
                _joinable_shared_columns(
                    columns_by_table[left_table] & columns_by_table[right_table]
                )
            )
            if not shared:
                continue
            inferred.append(
                JoinProfile(
                    from_table=left_table,
                    to_table=right_table,
                    column_pairs=[(column_name, column_name) for column_name in shared],
                    constraint_name=f"inferred_shared_columns:{left_table}:{right_table}",
                )
            )
    return inferred


def _joinable_shared_columns(column_names: set[str]) -> set[str]:
    ignored = {"created_at", "updated_at", "dt", "date", "biz_date", "calmonth"}
    return {
        name
        for name in column_names
        if name not in ignored
        and (
            name.endswith("_id") or name.endswith("_no") or name.endswith("_name") or "key" in name
        )
    }


def _time_columns(columns: list[ColumnProfile]) -> list[str]:
    return [
        column.column_name
        for column in columns
        if _infer_semantic_role(column) == SemanticRole.TIME
    ]


def _sample_values(column: ColumnProfile) -> list[object]:
    return [_truncate_sample_value(value) for value in column.sample_values[:_SAMPLE_VALUE_LIMIT]]


def _truncate_sample_value(value: object) -> object:
    if isinstance(value, str) and len(value) > _SAMPLE_TEXT_LIMIT:
        return value[:_SAMPLE_TEXT_LIMIT]
    return value


def _infer_key_role(column: ColumnProfile) -> KeyRole:
    if column.primary_key:
        return KeyRole.PRIMARY_KEY
    if column.foreign_key:
        return KeyRole.FOREIGN_KEY
    return KeyRole.NONE


def _infer_semantic_role(column: ColumnProfile) -> SemanticRole:
    name = column.column_name.lower()
    data_type = (column.data_type or "").lower()
    if any(token in name for token in ("date", "time", "dt", "month", "year")):
        return SemanticRole.TIME
    if column.primary_key or name == "id" or name.endswith("_id"):
        return SemanticRole.ID
    if any(token in name for token in ("flag", "is_", "status", "enabled")):
        return SemanticRole.FLAG
    if any(
        token in data_type for token in ("int", "decimal", "numeric", "float", "double", "real")
    ):
        return SemanticRole.MEASURE
    if any(token in data_type for token in ("char", "text", "string")):
        return SemanticRole.DIMENSION
    return SemanticRole.UNKNOWN


def _infer_unit(column: ColumnProfile) -> UnitType:
    name = column.column_name.lower()
    data_type = (column.data_type or "").lower()
    if any(token in name for token in ("date", "time", "dt")):
        return UnitType.TIME
    if any(token in name for token in ("amount", "price", "cost", "gmv", "fee")):
        return UnitType.CURRENCY
    if any(token in name for token in ("rate", "ratio", "percent", "pct")):
        return UnitType.PERCENT
    if any(
        token in data_type for token in ("int", "decimal", "numeric", "float", "double", "real")
    ):
        return UnitType.NUMBER
    return UnitType.NONE
