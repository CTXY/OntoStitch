"""Build Initial dimension-value assets from observed reference SQL literals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import expressions

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
from cortex.capabilities.sql_training.builders import build_dimension_value_assets
from cortex.capabilities.sql_training.contracts.references import (
    physical_reference_catalog,
)
from cortex.capabilities.sql_training.contracts.semantic_mapping import (
    coerce_sqlite_dqs_literals,
    column_allows_numeric_dimension_values,
    column_is_numeric,
)
from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.extractors.sql_normalizer import (
    physical_column_sources,
)
from cortex.capabilities.sql_training.models import (
    ColumnProperties,
    NodeAsset,
    Source,
    TableProperties,
)
from cortex.capabilities.sql_training.runtime_config import CortexConfig
from cortex.infrastructure.database import DatabaseConnector

_SUPPORTED_PREDICATES = (
    expressions.EQ,
    expressions.NEQ,
    expressions.In,
    expressions.Like,
)


@dataclass(frozen=True)
class PredicateColumnEvidence:
    source_id: str
    column_ref: str
    operator: str
    literals: tuple[str, ...]


@dataclass(frozen=True)
class DimensionValueBootstrapResult:
    selected_column_refs: tuple[str, ...]
    evidence: tuple[PredicateColumnEvidence, ...]
    dimension_value_count: int
    preserved_dimension_value_count: int
    skipped_unverified_literals: tuple[dict[str, Any], ...]
    manifest_path: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_column_refs": list(self.selected_column_refs),
            "evidence": [
                {
                    "source_id": item.source_id,
                    "column_ref": item.column_ref,
                    "operator": item.operator,
                    "literals": list(item.literals),
                }
                for item in self.evidence
            ],
            "dimension_value_count": self.dimension_value_count,
            "preserved_dimension_value_count": self.preserved_dimension_value_count,
            "skipped_unverified_literals": list(self.skipped_unverified_literals),
            "manifest_path": self.manifest_path,
        }


class TrainingDimensionValueBootstrap:
    def __init__(
        self,
        config: CortexConfig,
        workspace: Workspace,
        database: DatabaseConnector | None,
    ):
        self.config = config
        self.workspace = workspace
        self.database = database

    def run(
        self,
        records: list[dict[str, Any]],
        *,
        run_id: str | None = None,
    ) -> DimensionValueBootstrapResult:
        columns = self.workspace.read_draft_assets(NodeType.COLUMN)
        tables = self.workspace.read_draft_assets(NodeType.TABLE)
        column_by_ref = {asset.id: asset for asset in columns}
        if not column_by_ref:
            return DimensionValueBootstrapResult((), (), 0, 0, (), None)

        table_columns = _table_schema_from_columns(columns, tables)
        physical_catalog = physical_reference_catalog(table_columns)
        evidence = _collect_predicate_column_evidence(
            records,
            column_by_ref=column_by_ref,
            physical_catalog=physical_catalog,
            table_columns={table: set(names) for table, names in table_columns.items()},
            dialect=self.config.database.dialect,
        )
        selected_refs = tuple(sorted({item.column_ref for item in evidence}))
        if not selected_refs:
            manifest_path = self._write_manifest(run_id, evidence, [], [], [])
            return DimensionValueBootstrapResult(
                (), tuple(evidence), 0, 0, (), manifest_path
            )
        expanded_items: list[dict[str, Any]] = []
        skipped_unverified_literals: list[dict[str, Any]] = []
        matches_by_literal: dict[tuple[str, str], list[Any]] = {}
        for item in evidence:
            column_asset = column_by_ref[item.column_ref]
            table_name = _column_table_name(column_asset, tables)
            column_name = _column_name(column_asset)
            column_schema = _column_schema(column_asset, table_name=table_name)
            if column_is_numeric(item.column_ref, table_schema=column_schema) and not (
                column_allows_numeric_dimension_values(
                    item.column_ref,
                    table_schema=column_schema,
                )
            ):
                skipped_unverified_literals.extend(
                    {
                        "source_id": item.source_id,
                        "column_ref": item.column_ref,
                        "operator": item.operator,
                        "literal": value,
                        "reason": "non_categorical_numeric_literal",
                    }
                    for value in item.literals
                )
                continue
            for value in item.literals:
                if self.database is None:
                    raise ValueError(
                        "Dimension Value bootstrap requires a database connection"
                    )
                key = (item.column_ref, value)
                if key not in matches_by_literal:
                    matches_by_literal[key] = self.database.find_exact_column_values(
                        table_name, column_name, value
                    )
                matches = matches_by_literal[key]
                if not matches:
                    skipped_unverified_literals.append(
                        {
                            "source_id": item.source_id,
                            "column_ref": item.column_ref,
                            "operator": item.operator,
                            "literal": value,
                            "reason": "not_present_in_database",
                        }
                    )
                    continue
                if len(matches) != 1:
                    raise ValueError(
                        "Gold SQL Dimension Value is not uniquely present in the database: "
                        f"source_id={item.source_id!r}, column={table_name}.{column_name}, "
                        f"literal={value!r}, matches={matches!r}"
                    )
                expanded_items.append(
                    {
                        "table_name": table_name,
                        "column_name": column_name,
                        "value": matches[0],
                        "aliases": [value] if str(matches[0]) != value else [],
                        "source_type": "qa",
                        "source_id": item.source_id,
                    }
                )

        generated = _merge_dimension_values(
            build_dimension_value_assets(expanded_items)
        )
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
        preserved = self.workspace.read_draft_assets(NodeType.DIMENSION_VALUE)
        combined = _merge_dimension_values([*preserved, *generated])
        self.workspace.write_draft_assets(NodeType.DIMENSION_VALUE, combined)
        manifest_path = self._write_manifest(
            run_id,
            evidence,
            selected_refs,
            generated,
            skipped_unverified_literals,
        )
        return DimensionValueBootstrapResult(
            selected_column_refs=selected_refs,
            evidence=tuple(evidence),
            dimension_value_count=len(generated),
            preserved_dimension_value_count=len(preserved),
            skipped_unverified_literals=tuple(skipped_unverified_literals),
            manifest_path=manifest_path,
        )

    def _write_manifest(
        self,
        run_id: str | None,
        evidence: list[PredicateColumnEvidence],
        selected_refs: list[str] | tuple[str, ...],
        generated: list[NodeAsset],
        skipped_unverified_literals: list[dict[str, Any]],
    ) -> str:
        manifest_dir = self.workspace.logs_dir / "train" / (run_id or "bootstrap")
        manifest_path = manifest_dir / "dimension_value_bootstrap.json"
        rows_by_column: dict[str, int] = {}
        for asset in generated:
            for reference in asset.properties.references:
                rows_by_column[reference] = rows_by_column.get(reference, 0) + 1
        write_json_atomic(
            manifest_path,
            {
                "selected_column_refs": list(selected_refs),
                "dimension_value_counts": rows_by_column,
                "skipped_unverified_literals": skipped_unverified_literals,
                "evidence": [
                    {
                        "source_id": item.source_id,
                        "column_ref": item.column_ref,
                        "operator": item.operator,
                        "literals": list(item.literals),
                    }
                    for item in evidence
                ],
            },
        )
        return str(manifest_path)


def _collect_predicate_column_evidence(
    records: list[dict[str, Any]],
    *,
    column_by_ref: dict[str, NodeAsset],
    physical_catalog: Any,
    table_columns: dict[str, set[str]],
    dialect: str,
) -> list[PredicateColumnEvidence]:
    evidence: list[PredicateColumnEvidence] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for record in records:
        source_id = str(record.get("source_id") or "")
        sql = str(record.get("sql") or "")
        try:
            tree = sqlglot.parse_one(sql, dialect=dialect)
        except Exception as exc:
            raise ValueError(f"Failed to parse GT SQL for {source_id}: {exc}") from exc
        if tree is None:
            raise ValueError(f"Failed to parse GT SQL for {source_id}")
        if dialect.casefold() == "sqlite":
            tree, _ = coerce_sqlite_dqs_literals(
                tree,
                physical_catalog=physical_catalog,
            )
        lineage = physical_column_sources(tree, table_columns=table_columns)
        for predicate in tree.walk():
            if not isinstance(predicate, _SUPPORTED_PREDICATES):
                continue
            if isinstance(predicate, expressions.Like) and isinstance(
                predicate.parent, expressions.Not
            ):
                continue
            column, literals = _predicate_column_and_literals(predicate)
            if column is None or not literals:
                continue
            source = lineage.get(id(column))
            column_ref = make_node_id(NodeType.COLUMN, *source) if source else None
            if column_ref not in column_by_ref:
                continue
            operator = _predicate_operator(predicate)
            key = (source_id, column_ref, operator, literals)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                PredicateColumnEvidence(
                    source_id=source_id,
                    column_ref=column_ref,
                    operator=operator,
                    literals=literals,
                )
            )
    return sorted(
        evidence,
        key=lambda item: (
            item.column_ref,
            item.source_id,
            item.operator,
            item.literals,
        ),
    )


def _predicate_column_and_literals(
    predicate: expressions.Expression,
) -> tuple[expressions.Column | None, tuple[str, ...]]:
    if isinstance(predicate, expressions.Like):
        column = (
            predicate.this if isinstance(predicate.this, expressions.Column) else None
        )
        literal = predicate.expression
        if not isinstance(literal, expressions.Literal):
            return None, ()
        value = str(literal.this)
        if not _is_exact_like_literal(value):
            return None, ()
        return column, (value,)

    if isinstance(predicate, expressions.In):
        column = (
            predicate.this if isinstance(predicate.this, expressions.Column) else None
        )
        literals = tuple(
            str(item.this)
            for item in predicate.expressions
            if isinstance(item, expressions.Literal)
        )
        if len(literals) != len(predicate.expressions):
            return None, ()
        return column, literals

    left = predicate.this
    right = predicate.expression
    if isinstance(left, expressions.Column) and isinstance(right, expressions.Literal):
        return left, (str(right.this),)
    if isinstance(right, expressions.Column) and isinstance(left, expressions.Literal):
        return right, (str(left.this),)
    return None, ()


def _predicate_operator(predicate: expressions.Expression) -> str:
    if isinstance(predicate, expressions.In):
        return "NOT_IN" if isinstance(predicate.parent, expressions.Not) else "IN"
    if isinstance(predicate, expressions.Like):
        return "LIKE"
    if isinstance(predicate, expressions.NEQ):
        return "!="
    return "="


def _is_exact_like_literal(value: str) -> bool:
    return "%" not in value and "_" not in value


def _column_name(asset: NodeAsset) -> str:
    data = asset.properties
    if not isinstance(data, ColumnProperties):
        raise TypeError(f"Expected Column asset: {asset.id}")
    return data.column_name


def _column_schema(asset: NodeAsset, *, table_name: str) -> dict[str, str]:
    data = asset.properties
    if not isinstance(data, ColumnProperties):
        raise TypeError(f"Expected Column asset: {asset.id}")
    detail = " ".join(
        part for part in (data.data_type or "", asset.description or "") if part
    )
    return {table_name: f"- {data.column_name}: {detail}"}


def _table_schema_from_columns(
    column_assets: list[NodeAsset],
    table_assets: list[NodeAsset],
) -> dict[str, list[str]]:
    schema: dict[str, list[str]] = {}
    for asset in column_assets:
        schema.setdefault(_column_table_name(asset, table_assets), []).append(
            _column_name(asset)
        )
    return schema


def _column_table_name(asset: NodeAsset, table_assets: list[NodeAsset]) -> str:
    data = asset.properties
    if not isinstance(data, ColumnProperties):
        raise TypeError(f"Expected Column asset: {asset.id}")
    table_by_ref = {table.id: table for table in table_assets}
    table_ref = next((ref for ref in data.references if ref.startswith("table.")), None)
    table_asset = table_by_ref.get(table_ref or "")
    if table_asset is not None and isinstance(table_asset.properties, TableProperties):
        return table_asset.properties.table_name
    if table_ref:
        return table_ref.removeprefix("table.")
    name = asset.id.removeprefix("column.")
    table_name, separator, _ = name.rpartition(".")
    if not separator:
        raise ValueError(f"Column has no table reference: {asset.id}")
    return table_name


def _merge_dimension_values(assets: list[NodeAsset]) -> list[NodeAsset]:
    from cortex.capabilities.sql_training.graph.values import merge_verified_values

    return merge_verified_values([], assets)
