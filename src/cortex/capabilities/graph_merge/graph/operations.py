"""Exact graph operations that preserve source attribution and declared references."""

from __future__ import annotations

import json
import re
from typing import Any

PHYSICAL = frozenset({"table", "column", "dimension_value"})


def unique(values):
    return tuple(dict.fromkeys(values))


def remap_property_node_ids(value: Any, node_mappings: dict[str, str]) -> Any:
    """Remap declared reference slots, preserving prose and SQL literals."""
    from cortex.schema.references import remap_references

    return remap_references(value, node_mappings) if isinstance(value, dict) else value


def deduplicate_edges(edges):
    """Exact semantic assertions; physical JOIN identity is direction independent."""
    from cortex.schema.physical_joins import join_edge_key, merge_join_edges

    by_signature, mappings = {}, {}
    for edge in edges:
        # Only controller-generated catalog prose embeds temporary source IDs.
        # Canonicalize that template after endpoint remapping so identical
        # physical ownership assertions can share one edge and exact sources.
        if (
            edge.reference_origin == "default"
            and edge.edge_type in {"belongs_to", "value_of"}
            and isinstance(edge.properties.get("description"), str)
            and re.fullmatch(
                rf"\S+ {edge.edge_type} its catalog owner \S+\.",
                edge.properties.get("description", ""),
            )
        ):
            edge = edge.model_copy(
                update={
                    "properties": {
                        **edge.properties,
                        "description": f"{edge.from_node_id} {edge.edge_type} its catalog owner {edge.to_node_id}.",
                    }
                }
            )
        if edge.edge_type == "joins_with":
            signature = (edge.edge_type, join_edge_key(edge))
            previous = by_signature.get(signature)
            merged = merge_join_edges([previous] if previous else [], [edge])[0]
            by_signature[signature] = merged
            mappings[edge.id] = merged.id
            continue
        signature = (
            edge.edge_type,
            edge.from_node_id,
            edge.to_node_id,
            json.dumps(edge.properties, sort_keys=True, ensure_ascii=False),
        )
        previous = by_signature.get(signature)
        if previous:
            mappings[edge.id] = previous.id
            by_signature[signature] = previous.model_copy(
                update={"sources": unique((*previous.sources, *edge.sources))}
            )
        else:
            by_signature[signature] = edge
            mappings[edge.id] = edge.id
    return {e.id: e for e in by_signature.values()}, mappings


from cortex.schema.references import property_reference_ids, remap_references


def semantic_relations_first(edges, nodes):
    """Stable pagination: show business topology before physical grounding."""
    return sorted(
        edges,
        key=lambda e: any(
            nodes[key].node_type in PHYSICAL for key in (e.from_node_id, e.to_node_id)
        ),
    )


def remap_node(node, node_map, edge_map=None):
    """Only declared reference slots; never replace prose or SQL literals."""

    def properties(value):
        return remap_references(value, node_map)

    return node.model_copy(
        update={
            "id": node_map.get(node.id, node.id),
            # A varying name uses the owner's ID as a structural placeholder.
            # Alternative names and ordinary display names remain authored text.
            "name": node_map.get(node.id, node.id)
            if node.name == node.id
            and any("name" in a.overrides for a in node.alternatives)
            else node.name,
            "properties": properties(node.properties),
            "alternatives": tuple(
                a.model_copy(
                    update={
                        "overrides": {
                            key: properties({key.removeprefix("properties."): value})[
                                key.removeprefix("properties.")
                            ]
                            if key.startswith("properties.")
                            else value
                            for key, value in a.overrides.items()
                        }
                    }
                )
                for a in node.alternatives
            ),
        }
    )


def remap_edge(edge, node_map, edge_map=None):
    return edge.model_copy(
        update={
            "id": (edge_map or {}).get(edge.id, edge.id),
            "from_node_id": node_map.get(edge.from_node_id, edge.from_node_id),
            "to_node_id": node_map.get(edge.to_node_id, edge.to_node_id),
            "properties": remap_references(edge.properties, node_map),
        }
    )


def references(node):
    result = property_reference_ids(node.properties)
    for alternative in node.alternatives:
        props = {
            k.removeprefix("properties."): v
            for k, v in alternative.overrides.items()
            if k.startswith("properties.")
        }
        result |= property_reference_ids(props)
    return result


def decision_references(node, nodes, adjacent):
    """Recall through explicit Rule operands; never infer SQL or business identity."""
    result = references(node)
    if node.node_type == "generation_rule":
        for edge in adjacent:
            if edge.from_node_id == node.id and edge.edge_type == "applies_to":
                result.add(edge.to_node_id)
                operand = nodes.get(edge.to_node_id)
                if operand is not None:
                    result.update(references(operand))
    return result
