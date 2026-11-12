"""Preserve verified Value identity, aliases and provenance across materialization."""

from pydantic import Field

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel


class ValueWrite(NodeModel):
    column_id: str = Field(description="Exact ID of the existing owner Column.")
    value: str | int | float | bool = Field(
        description="Exact stored scalar, preserving type and case; code verifies it."
    )
    aliases: list[str] = Field(default_factory=list)


def value_asset(write: ValueWrite, assets: list[NodeAsset]) -> NodeAsset:
    """Allocate the same identity as automatic predicate materialization."""
    from cortex.capabilities.sql_training.builders.dimension_values import (
        build_dimension_value_assets,
    )

    by_id = {a.id: a for a in assets}
    column = by_id.get(write.column_id)
    if column is None or column.node_type != NodeType.COLUMN:
        raise ValueError("Value column_id must identify an existing Column")
    # Reuse verified legacy IDs rather than creating a second identity on write.
    for previous in assets:
        if (previous.node_type == NodeType.DIMENSION_VALUE
                and previous.properties.references == [column.id]
                and type(previous.properties.value) is type(write.value)
                and previous.properties.value == write.value):
            return previous.model_copy(update={
                "aliases": list(dict.fromkeys([*previous.aliases, *write.aliases]))
            })
    table = by_id[column.properties.references[0]]
    asset = build_dimension_value_assets(
        [
            {
                "table_name": table.properties.table_name,
                "column_name": column.properties.column_name,
                "value": write.value,
                "aliases": write.aliases,
            }
        ]
    )[0]
    return asset.model_copy(
        update={
            "properties": asset.properties.model_copy(
                update={"references": [column.id]}
            )
        }
    )


def verify_value(asset: NodeAsset, assets: list[NodeAsset], database) -> None:
    by_id = {a.id: a for a in assets}
    refs = asset.properties.references
    if (
        len(refs) != 1
        or refs[0] not in by_id
        or by_id[refs[0]].node_type != NodeType.COLUMN
    ):
        raise ValueError("Value requires exactly one existing Column reference")
    column = by_id[refs[0]]
    table = by_id[column.properties.references[0]]
    value = asset.properties.value
    if type(value) not in {str, int, float, bool}:
        raise ValueError("Value must be a non-null stored scalar")
    matches = database.find_exact_column_values(
        table.properties.table_name, column.properties.column_name, value
    )
    if len(matches) != 1 or type(matches[0]) is not type(value) or matches[0] != value:
        raise ValueError(
            f"Value must match exactly one typed stored value; observed={matches!r}"
        )
    canonical = value_asset(ValueWrite(column_id=column.id, value=value), assets)
    if asset.id != canonical.id:
        raise ValueError(
            "Use edit_graph.values to allocate the canonical Value ID, or reuse that returned ID"
        )


def merge_verified_values(
    existing: list[NodeAsset], updates: list[NodeAsset]
) -> list[NodeAsset]:
    by_id = {a.id: a for a in existing}
    for asset in updates:
        previous = by_id.get(asset.id)
        if previous is None:
            by_id[asset.id] = asset
            continue
        if previous.properties.references != asset.properties.references:
            raise ValueError("verified Value owner cannot change")
        if (
            type(previous.properties.value) is not type(asset.properties.value)
            or previous.properties.value != asset.properties.value
        ):
            raise ValueError("verified Value ID collides with a different stored value")
        sources = {
            (s.source_type, s.source_id): s for s in [*previous.sources, *asset.sources]
        }
        data = previous.model_dump(mode="json")
        data.update(
            aliases=list(dict.fromkeys([*previous.aliases, *asset.aliases])),
            sources=[s.model_dump(mode="json") for s in sources.values()],
        )
        by_id[asset.id] = NodeAsset.model_validate(data)
    return [by_id[key] for key in sorted(by_id)]
