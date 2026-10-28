"""Case-local verified Dimension Value materialization for Semantic Mappings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.capabilities.sql_training.builders import build_dimension_value_assets
from cortex.capabilities.sql_training.contracts.references import (
    physical_reference_catalog,
)
from cortex.capabilities.sql_training.contracts.semantic_mapping import (
    coerce_sqlite_dqs_literals,
    column_allows_numeric_dimension_values,
    column_is_numeric,
    normalize_semantic_mapping_candidate,
)
from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.models import NodeAsset, Source
from cortex.infrastructure.database import DatabaseConnector


@dataclass(frozen=True)
class ColumnLiteralObligation:
    column_ref: str
    literal: str
    operator: str


def materialize_semantic_mapping_dimension_values(
    assets: list[NodeAsset],
    *,
    table_schema: dict[str, Any],
    database: DatabaseConnector | None,
    source_id: str,
    existing_values: list[NodeAsset] | None = None,
) -> tuple[list[NodeAsset], list[NodeAsset]]:
    """Build DB-verified value nodes for exact Semantic Mapping predicates.

    Wildcard LIKE patterns are intentionally column-only: they express a pattern
    predicate, not a concrete stored value endpoint.
    """

    if database is None:
        return assets, []
    normalized_assets: list[NodeAsset] = []
    items: list[dict[str, Any]] = []
    existing_catalog = physical_reference_catalog(
        table_schema, dimension_values=existing_values or []
    )
    for asset in assets:
        if asset.node_type != NodeType.SEMANTIC_MAPPING:
            normalized_assets.append(asset)
            continue
        normalized = normalize_semantic_mapping_candidate(
            asset,
            table_schema={key: str(value) for key, value in table_schema.items()},
            physical_catalog=existing_catalog,
            dialect=database.get_dialect(),
        )
        normalized_assets.append(normalized)
        for obligation in extract_semantic_mapping_value_obligations(
            normalized,
            table_schema=table_schema,
        ):
            _, table_name, column_name = obligation.column_ref.split(".", 2)
            matches = database.find_exact_column_values(
                table_name,
                column_name,
                obligation.literal,
            )
            if len(matches) != 1:
                raise ValueError(
                    "Semantic Mapping Dimension Value is not uniquely present in the "
                    "database: "
                    f"source_id={source_id!r}, column={table_name}.{column_name}, "
                    f"literal={obligation.literal!r}, matches={matches!r}"
                )
            items.append(
                {
                    "table_name": table_name,
                    "column_name": column_name,
                    "value": matches[0],
                    "aliases": (
                        [obligation.literal]
                        if str(matches[0]) != obligation.literal
                        else []
                    ),
                    "source_type": SourceType.QA.value,
                    "source_id": source_id,
                }
            )
    generated = _merge_dimension_values(build_dimension_value_assets(items))
    generated = [
        asset.model_copy(
            update={
                "sources": [
                    *asset.sources,
                    Source(
                        source_type=SourceType.DATABASE,
                        source_id=next(
                            reference.removeprefix("column.")
                            for reference in asset.properties.references
                            if reference.startswith("column.")
                        ),
                    ),
                ]
            }
        )
        for asset in generated
    ]
    verified_catalog = physical_reference_catalog(
        table_schema, dimension_values=[*(existing_values or []), *generated]
    )
    normalized_assets = [
        normalize_semantic_mapping_candidate(
            asset, table_schema=table_schema, physical_catalog=verified_catalog,
            dialect=database.get_dialect(),
        )
        if asset.node_type == NodeType.SEMANTIC_MAPPING
        else asset
        for asset in normalized_assets
    ]
    return normalized_assets, generated


def extract_semantic_mapping_value_obligations(
    asset: NodeAsset,
    *,
    table_schema: dict[str, Any],
) -> tuple[ColumnLiteralObligation, ...]:
    if asset.node_type != NodeType.SEMANTIC_MAPPING:
        return ()
    physical_catalog = physical_reference_catalog(table_schema)
    obligations: list[ColumnLiteralObligation] = []
    seen: set[tuple[str, str, str]] = set()
    for target in asset.properties.targets:
        try:
            tree = sqlglot.parse_one(
                f"SELECT 1 WHERE {target.sql_expression}",
                dialect="sqlite",
            )
        except (sqlglot.errors.ParseError, ValueError) as exc:
            raise ValueError(
                f"Invalid Concept SQL expression: {target.sql_expression}"
            ) from exc
        if tree is None:
            continue
        tree, _ = coerce_sqlite_dqs_literals(tree, physical_catalog=physical_catalog)
        for predicate in tree.walk():
            if isinstance(predicate, expressions.EQ):
                pair = _column_literal_pair(
                    predicate, physical_catalog=physical_catalog
                )
                if pair is not None:
                    column_ref, literal = pair
                    literal_node = (
                        predicate.right
                        if isinstance(predicate.right, expressions.Literal)
                        else predicate.left
                    )
                    if _is_categorical_literal(
                        column_ref, literal_node, table_schema=table_schema
                    ):
                        _append_obligation(
                            obligations, seen, column_ref, literal, operator="="
                        )
            elif isinstance(predicate, expressions.In):
                column_ref, literals = _in_column_literals(
                    predicate,
                    physical_catalog=physical_catalog,
                )
                if column_ref is not None:
                    for literal, literal_node in zip(
                        literals, predicate.expressions, strict=True
                    ):
                        if not _is_categorical_literal(
                            column_ref, literal_node, table_schema=table_schema
                        ):
                            continue
                        _append_obligation(
                            obligations,
                            seen,
                            column_ref,
                            literal,
                            operator="IN",
                        )
            elif isinstance(predicate, expressions.Like):
                pair = _exact_like_column_literal(
                    predicate,
                    physical_catalog=physical_catalog,
                )
                if pair is not None:
                    _append_obligation(obligations, seen, *pair, operator="LIKE")
    return tuple(obligations)


def _is_categorical_literal(
    column_ref: str,
    literal: expressions.Expression,
    *,
    table_schema: dict[str, Any],
) -> bool:
    if isinstance(literal, expressions.Literal) and literal.is_string:
        return True
    schema = {key: str(value) for key, value in table_schema.items()}
    if not column_is_numeric(column_ref, table_schema=schema):
        return True
    return column_allows_numeric_dimension_values(column_ref, table_schema=schema)


def _append_obligation(
    obligations: list[ColumnLiteralObligation],
    seen: set[tuple[str, str, str]],
    column_ref: str,
    literal: str,
    *,
    operator: str,
) -> None:
    if literal.strip() == "":
        return
    key = (column_ref, literal, operator)
    if key in seen:
        return
    seen.add(key)
    obligations.append(
        ColumnLiteralObligation(
            column_ref=column_ref,
            literal=literal,
            operator=operator,
        )
    )


def _column_literal_pair(
    predicate: expressions.EQ,
    *,
    physical_catalog: Any,
) -> tuple[str, str] | None:
    left: Any = predicate.left
    right: Any = predicate.right
    if isinstance(left, expressions.Column) and isinstance(right, expressions.Literal):
        column_ref = _column_reference(left, physical_catalog=physical_catalog)
        return (column_ref, str(right.this)) if column_ref is not None else None
    if isinstance(right, expressions.Column) and isinstance(left, expressions.Literal):
        column_ref = _column_reference(right, physical_catalog=physical_catalog)
        return (column_ref, str(left.this)) if column_ref is not None else None
    return None


def _in_column_literals(
    predicate: expressions.In,
    *,
    physical_catalog: Any,
) -> tuple[str | None, tuple[str, ...]]:
    column_ref = (
        _column_reference(predicate.this, physical_catalog=physical_catalog)
        if isinstance(predicate.this, expressions.Column)
        else None
    )
    if column_ref is None or not predicate.expressions:
        return None, ()
    literals = tuple(
        str(item.this)
        for item in predicate.expressions
        if isinstance(item, expressions.Literal)
    )
    if len(literals) != len(predicate.expressions):
        return None, ()
    return column_ref, literals


def _exact_like_column_literal(
    predicate: expressions.Like,
    *,
    physical_catalog: Any,
) -> tuple[str, str] | None:
    if isinstance(predicate.parent, expressions.Not):
        return None
    left: Any = predicate.left
    right: Any = predicate.right
    if not isinstance(left, expressions.Column) or not isinstance(
        right, expressions.Literal
    ):
        return None
    literal = str(right.this)
    if not _is_exact_like_literal(literal):
        return None
    column_ref = _column_reference(left, physical_catalog=physical_catalog)
    return (column_ref, literal) if column_ref is not None else None


def _is_exact_like_literal(value: str) -> bool:
    return "%" not in value and "_" not in value


def _column_reference(
    column: expressions.Column,
    *,
    physical_catalog: Any,
) -> str | None:
    if not column.table or not column.name:
        return None
    reference = make_node_id(NodeType.COLUMN, column.table, column.name)
    resolver = getattr(physical_catalog, "resolve", None)
    resolved = resolver(reference) if callable(resolver) else None
    return resolved if isinstance(resolved, str) else None


def _merge_dimension_values(assets: list[NodeAsset]) -> list[NodeAsset]:
    from cortex.capabilities.sql_training.graph.values import merge_verified_values

    return merge_verified_values([], assets)
