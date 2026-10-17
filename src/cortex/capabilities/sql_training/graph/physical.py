from __future__ import annotations

from collections.abc import Iterable

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphSpace


class PhysicalGraphInitializationError(ValueError):
    """The existing SQL-training graph lacks its required physical topology."""


def validate_existing_physical_graph(graph: GraphSpace) -> None:
    """Require a complete producer-scoped physical graph whenever state is non-empty."""

    if not graph.nodes and not graph.edges:
        return
    tables = {node.id for node in graph.nodes if node.node_type == "table"}
    columns = {node.id: node for node in graph.nodes if node.node_type == "column"}
    if not tables or not columns:
        raise PhysicalGraphInitializationError(
            "non-empty SQL-training graph must contain initialized tables and columns"
        )
    belongs_by_column: dict[str, list[str]] = {}
    value_of_by_value: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.edge_type == "belongs_to":
            belongs_by_column.setdefault(edge.from_node_id, []).append(edge.to_node_id)
        elif edge.edge_type == "value_of":
            value_of_by_value.setdefault(edge.from_node_id, []).append(edge.to_node_id)
    for column_id, column in columns.items():
        table_ref = column.properties.get("table_ref")
        if table_ref not in tables:
            raise PhysicalGraphInitializationError(
                f"column {column_id!r} has no valid producer table_ref"
            )
        if belongs_by_column.get(column_id) != [table_ref]:
            raise PhysicalGraphInitializationError(
                f"column {column_id!r} must have exactly one matching belongs_to edge"
            )
    for value in (node for node in graph.nodes if node.node_type == "dimension_value"):
        column_ref = value.properties.get("column_ref")
        if column_ref not in columns:
            raise PhysicalGraphInitializationError(
                f"dimension value {value.id!r} has no valid producer column_ref"
            )
        if value_of_by_value.get(value.id) != [column_ref]:
            raise PhysicalGraphInitializationError(
                f"dimension value {value.id!r} must have exactly one matching value_of edge"
            )


def select_run_assets(
    assets: Iterable[NodeAsset],
    *,
    existing_graph: GraphSpace,
) -> tuple[NodeAsset, ...]:
    """Initialize all physical assets once, then emit only new values and semantic assets."""

    values = tuple(assets)
    if not existing_graph.nodes and not existing_graph.edges:
        return values
    validate_existing_physical_graph(existing_graph)
    existing_ids = {node.id for node in existing_graph.nodes}
    return tuple(
        asset
        for asset in values
        if asset.node_type not in {NodeType.TABLE, NodeType.COLUMN, NodeType.JOIN_PATH}
        and not (asset.node_type == NodeType.DIMENSION_VALUE and asset.id in existing_ids)
    )
