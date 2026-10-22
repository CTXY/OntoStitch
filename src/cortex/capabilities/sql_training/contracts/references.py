from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from cortex.capabilities.sql_training.contracts.metric import (
    metric_dependency_diagnostics,
    validate_metric_contract,
)
from cortex.capabilities.sql_training.contracts.semantic_mapping import (
    validate_semantic_mapping_candidate_contract,
    validate_semantic_mapping_schema_grounding,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.models import NodeAsset

_SCHEMA_COLUMN_LINE = re.compile(r"(?m)^\s*-\s*([^\n:]+?)\s*:")


def validate_asset_batch_references(
    assets: Iterable[NodeAsset],
    *,
    working_assets: Iterable[NodeAsset],
    table_schema: Any,
) -> None:
    """Validate an extracted batch before it can enter the draft Workspace.

    All members are visible together so a Metric may reference another Metric extracted in the
    same batch. Physical references still have to resolve against the runtime schema catalog.
    """

    batch = list(assets)
    working = {asset.id: asset for asset in working_assets}
    batch_by_id = {asset.id: asset for asset in batch}
    if len(batch_by_id) != len(batch):
        raise ValueError("extracted asset batch contains duplicate logical asset IDs")

    physical_ids = physical_reference_catalog(table_schema)
    if not physical_ids and not table_schema:
        physical_ids.update(
            reference
            for asset in batch
            for reference in _asset_reference_values(asset)
            if reference.startswith(("table.", "column.", "dimension_value."))
        )
    visible_ids = set(working) | set(batch_by_id) | physical_ids
    for asset in batch:
        _validate_asset_references(
            asset,
            visible_ids=visible_ids,
            physical_ids=physical_ids,
            table_schema=table_schema,
        )

    metrics = [asset for asset in batch if asset.node_type == NodeType.METRIC]
    diagnostics = metric_dependency_diagnostics(
        [asset for asset in working.values() if asset.node_type == NodeType.METRIC],
        metrics,
    )
    if diagnostics:
        diagnostic = diagnostics[0]
        raise ValueError(
            f"{diagnostic.code}: source={diagnostic.source_metric_id}; "
            f"targets={list(diagnostic.target_metric_ids)}; "
            f"cycle={list(diagnostic.cycle_path)}"
        )


def _validate_asset_references(
    asset: NodeAsset,
    *,
    visible_ids: set[str],
    physical_ids: set[str],
    table_schema: Any,
) -> None:
    if asset.node_type == NodeType.SEMANTIC_MAPPING:
        validate_semantic_mapping_candidate_contract(
            asset,
            table_schema=table_schema if isinstance(table_schema, dict) else {},
        )
        validate_semantic_mapping_schema_grounding(
            asset,
            table_schema=table_schema if isinstance(table_schema, dict) else {},
        )
        references = {
            reference
            for target in asset.properties.targets
            for reference in target.references
        }
        invalid = sorted(reference for reference in references if reference not in physical_ids)
        if invalid:
            raise ValueError(f"Concept references unresolved physical IDs: {invalid}")
        return

    if asset.node_type == NodeType.METRIC:
        validate_metric_contract(asset, require_generation_plan=True, require_pattern=True)
        references = set(asset.properties.references)
        unresolved = sorted(
            reference
            for reference in references
            if reference not in visible_ids and reference not in physical_ids
        )
        if unresolved:
            raise ValueError(f"Metric references unresolved IDs: {unresolved}")
        plan_references = {
            reference
            for plan in asset.properties.sql_generation_plans
            for reference in plan.schema_references()
        }
        unresolved_plan_references = sorted(
            reference for reference in plan_references if reference not in physical_ids
        )
        if unresolved_plan_references:
            raise ValueError(
                "Metric generation-plan physical tokens unresolved IDs: "
                f"{unresolved_plan_references}"
            )
        return

    if asset.node_type == NodeType.GENERATION_RULE:
        if asset.id in asset.properties.references:
            raise ValueError("Generation Rule cannot reference itself")
        nested_rules = sorted(
            reference
            for reference in asset.properties.references
            if reference.startswith("generation_rule.")
        )
        if nested_rules:
            raise ValueError(
                "Generation Rule cannot reference another Generation Rule: "
                f"{nested_rules}"
            )
        unresolved = sorted(
            reference
            for reference in asset.properties.references
            if reference not in set(visible_ids) and reference not in physical_ids
        )
        if unresolved:
            raise ValueError(f"Generation Rule references unresolved IDs: {unresolved}")
        unresolved_trigger_columns = sorted(
            reference
            for reference in asset.properties.triggers.sql.columns
            if reference not in physical_ids
        )
        if unresolved_trigger_columns:
            raise ValueError(
                "Generation Rule SQL trigger Columns unresolved IDs: "
                f"{unresolved_trigger_columns}"
            )


def physical_reference_catalog(table_schema: Any) -> set[str]:
    if not isinstance(table_schema, dict):
        return set()
    result: set[str] = set()
    aliases: dict[tuple[str, str], str] = {}
    for raw_table, raw_columns in table_schema.items():
        table = str(raw_table).strip()
        if not table:
            continue
        result.add(make_node_id(NodeType.TABLE, table))
        if isinstance(raw_columns, str):
            columns = _SCHEMA_COLUMN_LINE.findall(raw_columns)
        elif isinstance(raw_columns, dict):
            columns = [str(column) for column in raw_columns]
        elif isinstance(raw_columns, list):
            columns = [
                str(item.get("name") or item.get("column_name") or "")
                if isinstance(item, dict)
                else str(item)
                for item in raw_columns
            ]
        else:
            columns = []
        for raw_column in columns:
            column = raw_column.strip()
            if not column:
                continue
            canonical = make_node_id(NodeType.COLUMN, table, column)
            result.add(canonical)
            aliases[(table.casefold(), column.casefold())] = canonical
    # A canonical Dimension Value is valid when its owner Column is in the supplied catalog.
    # It is checked lazily because table_schema does not enumerate observed values.
    return _PhysicalCatalog(result, column_aliases=aliases)


class _PhysicalCatalog(set[str]):
    def __init__(
        self,
        values: Iterable[str] = (),
        *,
        column_aliases: dict[tuple[str, str], str] | None = None,
    ) -> None:
        super().__init__(values)
        self._column_aliases = column_aliases or {}

    def __contains__(self, item: object) -> bool:
        return self.resolve(item) is not None

    def resolve(self, item: object) -> str | None:
        if super().__contains__(item):
            return item if isinstance(item, str) else None
        if not isinstance(item, str):
            return None
        if item.startswith("column."):
            return self._resolve_column(item)
        if not item.startswith("dimension_value."):
            return None
        parts = item.split(".", 3)
        if len(parts) != 4:
            return None
        _, table_name, column_name, value = parts
        owner = self._resolve_column(f"column.{table_name}.{column_name}")
        if owner is None:
            return None
        _, canonical_table, canonical_column = owner.split(".", 2)
        return make_node_id(
            NodeType.DIMENSION_VALUE,
            canonical_table,
            canonical_column,
            value,
        )

    def _resolve_column(self, item: str) -> str | None:
        parts = item.split(".", 2)
        if len(parts) != 3 or not all(parts[1:]):
            return None
        _, table_name, column_name = parts
        canonical = self._column_aliases.get((table_name.casefold(), column_name.casefold()))
        if canonical is not None:
            return canonical
        candidate = make_node_id(NodeType.COLUMN, table_name, column_name)
        if super().__contains__(candidate):
            return candidate
        return None


def _asset_reference_values(asset: NodeAsset) -> set[str]:
    if asset.node_type == NodeType.SEMANTIC_MAPPING:
        return {
            reference
            for target in asset.properties.targets
            for reference in target.references
        }
    references = set(getattr(asset.properties, "references", []))
    if asset.node_type == NodeType.METRIC:
        references.update(
            reference
            for plan in asset.properties.sql_generation_plans
            for reference in plan.schema_references()
        )
    if asset.node_type == NodeType.GENERATION_RULE:
        references.update(asset.properties.triggers.sql.columns)
    return references
