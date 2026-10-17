from __future__ import annotations

import json
from typing import Any

from cortex.capabilities.graph_merge.models import EdgeNormalizationResult
from cortex.capabilities.graph_merge.normalization import (
    EdgePropertyMappingKey,
    edge_property_mapping_key,
)
from cortex.schema import (
    EdgeDirectionality,
    EdgeType,
    GraphEdge,
    GraphNode,
    GraphSchemaError,
    NodeMergeConflict,
    SchemaDefinition,
)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def remap_property_node_ids(value: Any, node_mappings: dict[str, str]) -> Any:
    """Rewrite exact node-ID values nested inside graph properties."""

    if isinstance(value, str):
        return node_mappings.get(value, value)
    if isinstance(value, dict):
        return {
            key: remap_property_node_ids(item, node_mappings)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [remap_property_node_ids(item, node_mappings) for item in value]
    if isinstance(value, tuple):
        return tuple(remap_property_node_ids(item, node_mappings) for item in value)
    return value


def edge_identity_key(edge: GraphEdge, edge_type: EdgeType) -> tuple[str, str, str, str]:
    identity = {field_id: edge.properties[field_id] for field_id in edge_type.identity_fields}
    from_node_id, to_node_id = edge.from_node_id, edge.to_node_id
    if edge_type.directionality == EdgeDirectionality.UNDIRECTED:
        from_node_id, to_node_id = sorted((from_node_id, to_node_id))
    return edge.edge_type, from_node_id, to_node_id, _canonical(identity)


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
        if (
            remapped.from_node_id not in node_by_id
            or remapped.to_node_id not in node_by_id
        ):
            raise GraphSchemaError(f"edge {edge.id!r} references an unknown endpoint after remap")
        if not _valid_endpoints(remapped, node_by_id, edge_types, schema):
            invalidated.append(edge.id)
            continue
        key = edge_identity_key(remapped, edge_types[remapped.edge_type])
        fingerprint = _canonical(remapped.properties)
        bucket = buckets.setdefault(key, {})
        matched = bucket.get(fingerprint)
        if matched is not None:
            output[matched.id] = matched.model_copy(
                update={"sources": _union_sources(matched.sources, remapped.sources)}
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
                    edge_property_mapping_key(edge.from_node_id, property_id, value), value
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
        if (
            remapped.from_node_id not in node_by_id
            or remapped.to_node_id not in node_by_id
        ):
            raise GraphSchemaError(f"edge {edge.id!r} references an unknown endpoint after remap")
        if not _valid_endpoints(remapped, node_by_id, edge_types, schema):
            invalidated.append(edge.id)
            mappings.append((edge.id, edge.id))
            continue
        edge_type = edge_types[remapped.edge_type]
        key = edge_identity_key(remapped, edge_type)
        fingerprint = _canonical(remapped.properties)
        bucket = buckets.setdefault(key, {})
        matched = bucket.get(fingerprint)
        if matched is not None:
            sources = _union_sources(matched.sources, remapped.sources)
            output[matched.id] = matched.model_copy(update={"sources": sources})
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
    edge_type = edge_types[edge.edge_type]
    source = node_by_id[edge.from_node_id]
    target = node_by_id[edge.to_node_id]
    return any(
        schema.is_node_type_compatible(source.node_type, expected)
        for expected in edge_type.from_node_types
    ) and any(
        schema.is_node_type_compatible(target.node_type, expected)
        for expected in edge_type.to_node_types
    )


def prune_stale_reference_edges(
    edges: tuple[GraphEdge, ...],
    nodes: tuple[GraphNode, ...],
    conflicts: tuple[NodeMergeConflict, ...] = (),
) -> tuple[tuple[GraphEdge, ...], tuple[str, ...]]:
    """Keep compiled dependency edges consistent with authoritative node references."""

    references_by_node_id = {
        node.id: _effective_reference_closure(node, conflicts) for node in nodes
    }
    kept = []
    dropped = []
    for edge in edges:
        references = references_by_node_id.get(edge.from_node_id)
        if (
            edge.edge_type in {"applies_to", "grounded_on"}
            and references is not None
            and edge.to_node_id not in references
        ):
            dropped.append(edge.id)
            continue
        kept.append(edge)
    return tuple(kept), tuple(sorted(dropped))


def _effective_reference_closure(
    node: GraphNode,
    conflicts: tuple[NodeMergeConflict, ...],
) -> frozenset[str] | None:
    references = _string_references(node.properties.get("references"))
    for conflict in conflicts:
        if conflict.node_id != node.id:
            continue
        for alternative in conflict.alternatives:
            properties = alternative.content.get("properties")
            if isinstance(properties, dict):
                references.update(_string_references(properties.get("references")))
    if "references" not in node.properties and not references:
        return None
    return frozenset(references)


def _string_references(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _union_sources(*groups):
    values = {}
    for group in groups:
        for source in group:
            key = (source.source_id, source.chunk_id, source.locator, source.case_id)
            values[key] = source
    keys = sorted(values, key=lambda item: tuple(x or "" for x in item))
    return tuple(values[key] for key in keys)
