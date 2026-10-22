"""Preserve verified Value identity, aliases and provenance across materialization."""

from cortex.capabilities.sql_training.models import NodeAsset


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
        if previous.properties.value != asset.properties.value:
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
