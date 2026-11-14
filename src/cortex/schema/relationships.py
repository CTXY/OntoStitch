"""Deterministic relationships shared by GraphManager and graph merge."""

from __future__ import annotations

import hashlib
import json

from cortex.schema.graph import (
    GraphEdge,
    GraphNode,
    NodeMergeConflict,
)
from cortex.schema.models import EdgeDirectionality, EdgeType, SchemaDefinition
from cortex.schema.validation import GraphSchemaError

MANAGED_RELATIONS = {
    "metric": frozenset(),
    "generation_rule": frozenset(),
    "concept": frozenset(),
    "event": frozenset(),
    "column": frozenset({"belongs_to"}),
    "dimension_value": frozenset({"value_of"}),
}


def reference_relation_type(source_type: str, target_type: str) -> str:
    """Default edge for a refs anchor; shared by compilation and authoring guides."""
    if source_type == "column" and target_type == "table":
        return "belongs_to"
    if source_type == "dimension_value" and target_type == "column":
        return "value_of"
    if source_type in {"concept", "event"} and target_type in {"column", "dimension_value"}:
        return "grounded_on"
    if source_type == "metric" and target_type == "metric":
        return "depends_on"
    if source_type == "generation_rule" and target_type in {"concept", "event", "metric"}:
        return "applies_to"
    if source_type in {"metric", "generation_rule"} and target_type in {
        "table",
        "column",
        "dimension_value",
    }:
        return "grounded_on"
    raise GraphSchemaError(
        f"unsupported compiled reference type {target_type!r} for {source_type!r}"
    )


def is_dependency_edge(edge: GraphEdge) -> bool:
    """Read dependency semantics from a saved relation, never from node refs."""
    return edge.reference_origin is not None or edge.edge_type in {
        "grounded_on",
        "depends_on",
        "applies_to",
        "belongs_to",
        "value_of",
        "computed_from",
        "filters_on",
    }


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def edge_identity_key(edge: GraphEdge, edge_type: EdgeType | None) -> tuple:
    """Stable relation identity; physical JOIN keys are complete and reversible."""
    if edge.edge_type == "joins_with":
        from cortex.schema.physical_joins import join_edge_key

        return edge.edge_type, join_edge_key(edge)
    endpoints = (edge.from_node_id, edge.to_node_id)
    if edge_type and edge_type.directionality == EdgeDirectionality.UNDIRECTED:
        endpoints = tuple(sorted(endpoints))
    fields = edge_type.identity_fields if edge_type else ()
    identity = {name: edge.properties.get(name) for name in fields}
    if "target_id" in edge.properties:
        identity["target_id"] = edge.properties["target_id"]
    return edge.edge_type, *endpoints, _canonical(identity)


def _valid_endpoints(edge, nodes, edge_types, schema):
    spec = edge_types[edge.edge_type]
    return any(
        schema.is_node_type_compatible(nodes[edge.from_node_id].node_type, t)
        for t in spec.from_node_types
    ) and any(
        schema.is_node_type_compatible(nodes[edge.to_node_id].node_type, t)
        for t in spec.to_node_types
    )


def synchronize_reference_edges(
    edges: tuple[GraphEdge, ...],
    nodes: tuple[GraphNode, ...],
    conflicts: tuple[NodeMergeConflict | GraphNode, ...] = (),
    *,
    schema: SchemaDefinition,
    node_ids: set[str] | None = None,
) -> tuple[tuple[GraphEdge, ...], tuple[str, ...]]:
    """Create catalog ownership only; semantic relations are explicitly authored.

    A target match is not an outgoing-edge list. Formula refs are checked by
    GraphManager rather than used to invent business facts during reads/writes.
    """
    result = list(edges)
    for node in nodes:
        if node_ids is not None and node.id not in node_ids:
            continue
        field, relation = {
            "column": ("table_ref", "belongs_to"),
            "dimension_value": ("column_ref", "value_of"),
        }.get(node.node_type, (None, None))
        if not field or not node.properties.get(field):
            continue
        target = node.properties[field]
        properties = {"description": f"{node.id} {relation} its catalog owner {target}."}
        if node.node_type == "column":
            owner = next((item for item in nodes if item.id == target), None)
            data = owner.properties if owner else {}
            names = {node.id, node.properties.get("column_name")} - {None}
            properties.update({
                "is_default_time": data.get("default_time_column") in names,
                "is_important": bool(names.intersection(data.get("important_columns", ()))),
                "is_available_time": bool(names.intersection(data.get("available_time_columns", ()))),
                "is_primary_key": bool(names.intersection(data.get("primary_keys", ()))),
                "is_foreign_key": bool(names.intersection(data.get("foreign_keys", ()))),
            })
        existing = next((e for e in result if e.from_node_id == node.id
                         and e.to_node_id == target and e.edge_type == relation), None)
        if existing:
            if existing.reference_origin == "default":
                result[result.index(existing)] = existing.model_copy(update={
                    "properties": properties,
                    "sources": _union_sources(existing.sources, node.sources),
                })
            continue
        digest = hashlib.sha256(f"{node.id}:{relation}:{target}".encode()).hexdigest()[:24]
        result.append(GraphEdge(
            id=f"edge.catalog.{digest}", edge_type=relation,
            from_node_id=node.id, to_node_id=target,
            properties=properties,
            sources=node.sources, reference_origin="default",
        ))
    return tuple(sorted(result, key=lambda edge: (edge.edge_type, edge.id))), ()


def _union_sources(*groups):
    values = {}
    for group in groups:
        for source in group:
            key = (source.source_id, source.chunk_id, source.locator, source.case_id)
            values[key] = source
    keys = sorted(values, key=lambda item: tuple(x or "" for x in item))
    return tuple(values[key] for key in keys)
