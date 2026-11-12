from __future__ import annotations

import json
from typing import Any

from cortex.capabilities.graph_merge.models import EdgeNormalizationResult
from cortex.capabilities.graph_merge.normalization import (
    EdgePropertyMappingKey,
    edge_property_mapping_key,
)
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSchemaError,
    SchemaDefinition,
)
from cortex.schema.physical_joins import join_edge_key, merge_join_edges
from cortex.schema.relationships import _union_sources, edge_identity_key


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def remap_property_node_ids(value: Any, node_mappings: dict[str, str]) -> Any:
    """Remap declared reference slots, preserving prose and SQL literals."""
    from cortex.schema.references import remap_references

    return remap_references(value, node_mappings) if isinstance(value, dict) else value


def normalize_edges(
    *,
    existing: tuple[GraphEdge, ...],
    incoming: tuple[GraphEdge, ...],
    nodes: tuple[GraphNode, ...],
    node_mappings: dict[str, str],
    property_value_mappings: dict[EdgePropertyMappingKey, Any] | None = None,
    schema: SchemaDefinition,
) -> EdgeNormalizationResult:
    edge_types = schema.edge_type_index()
    node_by_id = {node.id: node for node in nodes}
    buckets: dict[tuple[str, str, str, str], dict[str, GraphEdge]] = {}
    output: dict[str, GraphEdge] = {}
    physical: dict[tuple, GraphEdge] = {}
    invalidated: list[str] = []
    for edge in sorted(existing, key=lambda item: (item.edge_type, item.id)):
        remapped = edge.model_copy(
            update={
                "from_node_id": node_mappings.get(edge.from_node_id, edge.from_node_id),
                "to_node_id": node_mappings.get(edge.to_node_id, edge.to_node_id),
                "properties": {
                    key: (property_value_mappings or {}).get(
                        edge_property_mapping_key(edge.from_node_id, key, value), value
                    )
                    for key, value in edge.properties.items()
                },
            }
        )
        remapped = remapped.model_copy(
            update={
                "properties": remap_property_node_ids(
                    remapped.properties, node_mappings
                )
            }
        )
        if (
            remapped.from_node_id not in node_by_id
            or remapped.to_node_id not in node_by_id
        ):
            raise GraphSchemaError(
                f"edge {edge.id!r} references an unknown endpoint after remap"
            )
        if not _valid_endpoints(remapped, node_by_id, edge_types, schema):
            invalidated.append(edge.id)
            continue
        if remapped.edge_type == "joins_with":
            key = join_edge_key(remapped)
            previous = physical.get(key)
            (merged,) = merge_join_edges([previous] if previous else [], [remapped])
            physical[key] = merged
            output[merged.id] = merged
            continue
        key = edge_identity_key(remapped, edge_types.get(remapped.edge_type))
        fingerprint = _canonical(remapped.properties)
        bucket = buckets.setdefault(key, {})
        matched = bucket.get(fingerprint)
        if matched is not None:
            output[matched.id] = matched.model_copy(
                update={
                    "sources": _union_sources(matched.sources, remapped.sources),
                    "reference_origin": "explicit"
                    if "explicit"
                    in {matched.reference_origin, remapped.reference_origin}
                    else matched.reference_origin or remapped.reference_origin,
                }
            )
            bucket[fingerprint] = output[matched.id]
            continue
        bucket[fingerprint] = remapped
        output[remapped.id] = remapped
    mappings: list[tuple[str, str]] = []
    variants: list[str] = []
    for edge in sorted(incoming, key=lambda item: (item.edge_type, item.id)):
        properties = dict(edge.properties)
        if property_value_mappings:
            properties = {
                property_id: property_value_mappings.get(
                    edge_property_mapping_key(edge.from_node_id, property_id, value),
                    value,
                )
                for property_id, value in properties.items()
            }
        remapped = edge.model_copy(
            update={
                "from_node_id": node_mappings.get(edge.from_node_id, edge.from_node_id),
                "to_node_id": node_mappings.get(edge.to_node_id, edge.to_node_id),
                "properties": properties,
            }
        )
        remapped = remapped.model_copy(
            update={
                "properties": remap_property_node_ids(
                    remapped.properties, node_mappings
                )
            }
        )
        if (
            remapped.from_node_id not in node_by_id
            or remapped.to_node_id not in node_by_id
        ):
            raise GraphSchemaError(
                f"edge {edge.id!r} references an unknown endpoint after remap"
            )
        if not _valid_endpoints(remapped, node_by_id, edge_types, schema):
            invalidated.append(edge.id)
            mappings.append((edge.id, edge.id))
            continue
        if remapped.edge_type == "joins_with":
            key = join_edge_key(remapped)
            previous = physical.get(key)
            (merged,) = merge_join_edges([previous] if previous else [], [remapped])
            physical[key] = merged
            output[merged.id] = merged
            mappings.append((edge.id, merged.id))
            continue
        edge_type = edge_types.get(remapped.edge_type)
        key = edge_identity_key(remapped, edge_type)
        fingerprint = _canonical(remapped.properties)
        bucket = buckets.setdefault(key, {})
        matched = bucket.get(fingerprint)
        if matched is not None:
            sources = _union_sources(matched.sources, remapped.sources)
            output[matched.id] = matched.model_copy(
                update={
                    "sources": sources,
                    "reference_origin": "explicit"
                    if "explicit"
                    in {matched.reference_origin, remapped.reference_origin}
                    else matched.reference_origin or remapped.reference_origin,
                }
            )
            bucket[fingerprint] = output[matched.id]
            mappings.append((edge.id, matched.id))
            continue
        if bucket:
            variants.append(edge.id)
        bucket[fingerprint] = remapped
        output[remapped.id] = remapped
        mappings.append((edge.id, remapped.id))
    result = tuple(sorted(output.values(), key=lambda item: (item.edge_type, item.id)))
    return EdgeNormalizationResult(
        edges=result,
        mappings=tuple(mappings),
        variant_edge_ids=tuple(variants),
        invalidated_edge_ids=tuple(sorted(invalidated)),
    )


def _valid_endpoints(edge, node_by_id, edge_types, schema):
    edge_type = edge_types.get(edge.edge_type)
    if edge_type is None:
        return True
    source = node_by_id[edge.from_node_id]
    target = node_by_id[edge.to_node_id]
    return any(
        schema.is_node_type_compatible(source.node_type, expected)
        for expected in edge_type.from_node_types
    ) and any(
        schema.is_node_type_compatible(target.node_type, expected)
        for expected in edge_type.to_node_types
    )
