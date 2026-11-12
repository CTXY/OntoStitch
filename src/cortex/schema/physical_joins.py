"""Shared physical relationship identity, provenance and constrained cardinality."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from cortex.schema.graph import GraphEdge

_REVERSE_CARDINALITY = {"1:1": "1:1", "1:N": "N:1", "N:1": "1:N"}


def _public_join_edge(edge: GraphEdge) -> GraphEdge:
    if edge.edge_type != "joins_with":
        raise ValueError("physical join merge only accepts joins_with")
    if set(edge.properties) - {"column_pairs", "cardinality", "description"}:
        raise ValueError(
            "obsolete/unsupported physical join properties; rebuild the physical graph"
        )
    pairs = edge.properties.get("column_pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("join edge requires complete column_pairs")
    normalized = set()
    for pair in pairs:
        if not isinstance(pair, dict) or set(pair) != {
            "from_column_ref",
            "to_column_ref",
        }:
            raise ValueError(
                "join column pair requires from_column_ref and to_column_ref"
            )
        left, right = pair["from_column_ref"], pair["to_column_ref"]
        if not isinstance(left, str) or not isinstance(right, str):
            raise TypeError("join column references must be strings")
        normalized.add((left, right))
    cardinality = edge.properties.get("cardinality")
    if cardinality is not None and cardinality not in _REVERSE_CARDINALITY:
        raise ValueError("unknown cardinality must be omitted")
    properties = {
        "column_pairs": [
            {"from_column_ref": left, "to_column_ref": right}
            for left, right in sorted(normalized)
        ]
    }
    properties["description"] = edge.properties.get("description") or (
        f"{edge.from_node_id} and {edge.to_node_id} are linked by the recorded equality column pairs. "
        "This connection alone does not imply business classification or observed cardinality."
    )
    if cardinality is not None:
        properties["cardinality"] = cardinality
    return edge.model_copy(update={"properties": properties})


def join_edge_key(edge: GraphEdge) -> tuple:
    edge = _public_join_edge(edge)
    return (
        tuple(sorted((edge.from_node_id, edge.to_node_id))),
        tuple(
            sorted(
                tuple(sorted((p["from_column_ref"], p["to_column_ref"])))
                for p in edge.properties["column_pairs"]
            )
        ),
    )


def merge_join_edges(
    existing: Iterable[GraphEdge], incoming: Iterable[GraphEdge]
) -> list[GraphEdge]:
    """Merge identity and provenance, reconciling constrained cardinality by direction."""
    by_key = {}
    used_ids = set()
    for edge in (*tuple(existing), *tuple(incoming)):
        edge = _public_join_edge(edge)
        key = join_edge_key(edge)
        current = by_key.get(key)
        if current is not None:
            incoming_cardinality = edge.properties.get("cardinality")
            if incoming_cardinality and edge.from_node_id != current.from_node_id:
                incoming_cardinality = _REVERSE_CARDINALITY[incoming_cardinality]
            old_cardinality = current.properties.get("cardinality")
            if (
                old_cardinality
                and incoming_cardinality
                and old_cardinality != incoming_cardinality
            ):
                raise ValueError(
                    "conflicting database-constraint cardinality for one relationship"
                )
            properties = dict(current.properties)
            if old_cardinality or incoming_cardinality:
                properties["cardinality"] = old_cardinality or incoming_cardinality
            sources = {
                (s.source_id, s.locator, s.case_id, s.chunk_id): s
                for s in (*current.sources, *edge.sources)
            }
            by_key[key] = current.model_copy(
                update={"properties": properties, "sources": tuple(sources.values())}
            )
            continue
        if edge.id in used_ids:
            edge = edge.model_copy(update={"id": _edge_id(key)})
        by_key[key] = edge
        used_ids.add(edge.id)
    return list(by_key.values())


def _edge_id(key) -> str:
    return "edge_" + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
