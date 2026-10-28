"""Deterministic relationships shared by GraphManager and graph merge."""

from __future__ import annotations

import hashlib
import json

from cortex.schema.graph import (
    GraphEdge,
    GraphNode,
    NodeMergeConflict,
    alternative_content,
)
from cortex.schema.models import EdgeDirectionality, EdgeType, SchemaDefinition
from cortex.schema.references import content_reference_ids
from cortex.schema.validation import GraphSchemaError

MANAGED_RELATIONS = {
    "metric": frozenset({"depends_on", "grounded_on"}),
    "generation_rule": frozenset({"applies_to", "grounded_on"}),
    "concept": frozenset({"grounded_on"}),
    "column": frozenset({"belongs_to"}),
    "dimension_value": frozenset({"value_of"}),
}


def reference_relation_type(source_type: str, target_type: str) -> str:
    """Default edge for a refs anchor; shared by compilation and authoring guides."""
    if source_type == "column" and target_type == "table":
        return "belongs_to"
    if source_type == "dimension_value" and target_type == "column":
        return "value_of"
    if source_type == "concept" and target_type in {"column", "dimension_value"}:
        return "grounded_on"
    if source_type == "metric" and target_type == "metric":
        return "depends_on"
    if source_type == "generation_rule" and target_type in {"concept", "metric"}:
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
    """Synchronize compiled dependencies for changed definitions and alternatives."""

    # The optional third argument is an import/proposal compatibility boundary.
    # Runtime synchronization reads candidates from their owner nodes.
    if conflicts:
        from cortex.schema.graph import GraphSpace
        from cortex.schema.graph_io import with_conflicts

        nodes = with_conflicts(
            GraphSpace(nodes=nodes, edges=edges), conflicts
        ).graph.nodes
    managed = MANAGED_RELATIONS
    by_id = {node.id: node for node in nodes}
    by_conflict = {node.id: node for node in nodes if node.alternatives}
    edge_types = schema.edge_type_index()
    desired = {}

    alternative_id = None

    def add(node, reference, edge_type, properties, sources):
        edge = GraphEdge(
            id="pending",
            edge_type=edge_type,
            from_node_id=node.id,
            to_node_id=reference,
            properties=properties,
            sources=sources,
            reference_origin="default",
        )
        if edge_type not in edge_types or not _valid_endpoints(
            edge, by_id, edge_types, schema
        ):
            raise GraphSchemaError(
                f"invalid compiled relationship {node.id!r} -> {reference!r}"
            )
        signature = (edge_type, node.id, reference, _canonical(edge.properties))
        old = desired.get(signature)
        alternatives = set(old.properties.get("alternative_ids", ())) if old else set()
        if alternative_id:
            alternatives.add(alternative_id)
        if alternatives:
            edge = edge.model_copy(
                update={
                    "properties": {
                        **edge.properties,
                        "alternative_ids": sorted(alternatives),
                    }
                }
            )
        desired[signature] = edge.model_copy(
            update={"sources": _union_sources(old.sources if old else (), sources)}
        )

    def target(reference):
        if reference not in by_id:
            raise GraphSchemaError(f"compiled reference {reference!r} has no node")
        kind = by_id[reference].node_type
        return next(
            (
                base
                for base in ("metric", "concept", "table", "column", "dimension_value")
                if schema.is_node_type_compatible(kind, base)
            ),
            kind,
        )

    for node in nodes:
        if node.node_type not in managed or (
            node_ids is not None and node.id not in node_ids
        ):
            continue
        conflict = by_conflict.get(node.id)
        branches = (
            [
                (alternative_content(node, a).properties, a.sources, a.alternative_id)
                for a in conflict.alternatives
            ]
            if conflict
            else [(node.properties, node.sources, None)]
        )
        for properties, sources, alternative_id in branches:
            if node.node_type == "column":
                reference = properties.get("table_ref")
                if reference is not None:
                    table = by_id.get(reference)
                    if table is None:
                        raise GraphSchemaError(
                            f"compiled reference {reference!r} has no node"
                        )
                    data = table.properties
                    names = {node.id, properties.get("column_name")} - {None}
                    flags = {
                        "is_default_time": data.get("default_time_column") in names,
                        "is_important": any(
                            x in names for x in data.get("important_columns", ())
                        ),
                        "is_available_time": any(
                            x in names for x in data.get("available_time_columns", ())
                        ),
                        "is_primary_key": any(
                            x in names for x in data.get("primary_keys", ())
                        ),
                        "is_foreign_key": any(
                            x in names for x in data.get("foreign_keys", ())
                        ),
                    }
                    add(node, reference, "belongs_to", flags, sources)
                continue
            if node.node_type == "dimension_value":
                reference = properties.get("column_ref")
                if reference is not None:
                    add(node, reference, "value_of", {}, sources)
                continue
            if node.node_type == "concept":
                for binding in properties.get("targets", ()):
                    refs = binding.get("references", ())
                    for reference in sorted(set(refs)):
                        add(
                            node,
                            reference,
                            reference_relation_type("concept", target(reference)),
                            {"target_id": binding["id"]},
                            sources,
                        )
                continue
            for reference in sorted(content_reference_ids(properties)):
                kind = target(reference)
                relation = reference_relation_type(node.node_type, kind)
                add(node, reference, relation, {}, sources)

    # A slot identifies an anchored connection, independently of its relation name.
    def slot(edge):
        return edge.from_node_id, edge.to_node_id, edge.properties.get("target_id")

    defaults = {slot(edge): edge for edge in desired.values()}
    active = {node.id for node in nodes if node_ids is None or node.id in node_ids}
    explicit, previous_defaults, dropped = [], {}, []
    for edge in edges:
        if edge.from_node_id not in active:
            explicit.append(edge)
            continue
        if edge.reference_origin == "default":
            previous_defaults.setdefault(slot(edge), []).append(edge)
            continue
        key = slot(edge)
        matches = [candidate for candidate in defaults if candidate[:2] == key[:2]]
        if key not in defaults and key[2] is None and matches:
            if len(matches) != 1:
                raise GraphSchemaError(
                    f"edge {edge.id!r} requires target_id to identify its Concept target"
                )
            key = matches[0]
            edge = edge.model_copy(
                update={
                    "properties": {
                        **edge.properties,
                        "target_id": key[2],
                    }
                }
            )
        if key in defaults:
            properties = dict(edge.properties)
            branches = defaults[key].properties.get("alternative_ids")
            if branches:
                supplied = properties.get("alternative_ids", branches)
                if edge.reference_origin == "explicit" and isinstance(supplied, list):
                    supplied = [value for value in supplied if value in branches]
                    if not supplied:
                        dropped.append(edge.id)
                        continue
                if (
                    not isinstance(supplied, list)
                    or not supplied
                    or any(
                        not isinstance(value, str) or value not in branches
                        for value in supplied
                    )
                ):
                    raise GraphSchemaError(
                        f"edge {edge.id!r} has invalid alternative_ids for its reference anchor"
                    )
                properties["alternative_ids"] = sorted(set(supplied))
            elif "alternative_ids" in properties:
                raise GraphSchemaError(
                    f"edge {edge.id!r} has branch metadata without a conflicted owner"
                )
            edge = edge.model_copy(
                update={"reference_origin": "explicit", "properties": properties}
            )
        elif edge.reference_origin == "explicit":
            # Updating an owner removes relationships whose anchor was removed.
            dropped.append(edge.id)
            continue
        explicit.append(edge)
    overridden = {
        slot(edge) for edge in explicit if edge.reference_origin == "explicit"
    }
    kept = list(explicit)
    occupied = {edge.id for edge in edges}
    for key, default in sorted(defaults.items(), key=lambda item: _canonical(item[0])):
        prior = previous_defaults.pop(key, [])
        if key in overridden:
            branches = set(default.properties.get("alternative_ids", ()))
            covered = {
                branch
                for edge in explicit
                if slot(edge) == key
                for branch in edge.properties.get("alternative_ids", ())
            }
            remaining = branches - covered
            if not remaining:
                dropped.extend(edge.id for edge in prior)
                continue
            default = default.model_copy(
                update={
                    "properties": {
                        **default.properties,
                        "alternative_ids": sorted(remaining),
                    }
                }
            )
        matching = next(
            (
                edge
                for edge in prior
                if edge.edge_type == default.edge_type
                and edge.properties == default.properties
            ),
            None,
        )
        if matching:
            kept.append(
                default.model_copy(
                    update={
                        "id": matching.id,
                        "sources": _union_sources(matching.sources, default.sources),
                    }
                )
            )
            dropped.extend(edge.id for edge in prior if edge.id != matching.id)
        else:
            dropped.extend(edge.id for edge in prior)
            digest = hashlib.sha256(
                _canonical((key, default.edge_type, default.properties)).encode()
            ).hexdigest()[:24]
            edge_id = f"edge.merge.{digest}"
            while edge_id in occupied:
                edge_id += ".new"
            occupied.add(edge_id)
            kept.append(default.model_copy(update={"id": edge_id}))
    dropped.extend(edge.id for prior in previous_defaults.values() for edge in prior)
    return tuple(sorted(kept, key=lambda edge: (edge.edge_type, edge.id))), tuple(
        sorted(dropped)
    )


def _union_sources(*groups):
    values = {}
    for group in groups:
        for source in group:
            key = (source.source_id, source.chunk_id, source.locator, source.case_id)
            values[key] = source
    keys = sorted(values, key=lambda item: tuple(x or "" for x in item))
    return tuple(values[key] for key in keys)
