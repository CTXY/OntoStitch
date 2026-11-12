from __future__ import annotations

from typing import Any

from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.capabilities.sql_training.models import (
    DimensionValueProperties,
    NodeAsset,
    Source,
)


def build_dimension_value_assets(items: list[dict[str, Any]]) -> list[NodeAsset]:
    assets: list[NodeAsset] = []
    for item in items:
        table_name = item["table_name"]
        column_name = item["column_name"]
        value = item["value"]
        column_ref = make_node_id(NodeType.COLUMN, table_name, column_name)
        assets.append(
            NodeAsset(
                id=make_node_id(
                    NodeType.DIMENSION_VALUE, table_name, column_name, value
                ),
                node_type=NodeType.DIMENSION_VALUE,
                name=str(value) if str(value).strip() else repr(value),
                aliases=[str(alias) for alias in item.get("aliases", [])],
                properties=DimensionValueProperties(
                    references=[column_ref],
                    value=value,
                ),
                sources=[
                    Source(
                        source_type=SourceType(
                            item.get("source_type", SourceType.INFERRED)
                        ),
                        source_id=item.get("source_id"),
                    )
                ],
            )
        )
    return assets
