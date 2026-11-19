"""Inline conflict graph I/O; legacy sidecars are accepted only at import."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from cortex.schema.graph import (
    GraphNode,
    GraphSpace,
    GraphState,
    MergeStatus,
    NodeMergeConflict,
    alternative_content,
)

_CONFLICT_FIELDS = frozenset(
    {"consolidation_status", "preservation_reasons", "alternatives", "conflict_points"}
)


def import_inline_graph(value: Any) -> Any:
    """Normalize legacy envelopes into graph nodes without keeping a sidecar."""
    if not isinstance(value, Mapping):
        return value
    plain = "nodes" in value or "edges" in value
    if plain:
        unknown = set(value) - {"nodes", "edges", "relation_contracts"}
        if unknown:
            raise ValueError(f"unknown inline graph fields: {sorted(unknown)}")
        payload = {"graph": value}
    else:
        payload = dict(value)
    graph_value = payload.get("graph")
    if isinstance(graph_value, Mapping):
        for node in graph_value.get("nodes", ()):
            if isinstance(node, Mapping):
                present = _CONFLICT_FIELDS.intersection(node)
                if present and present != _CONFLICT_FIELDS:
                    raise ValueError(
                        "inline conflict nodes require all conflict metadata fields"
                    )
        graph = GraphSpace.model_validate(graph_value)
    elif isinstance(graph_value, GraphSpace):
        graph = graph_value
    else:
        return value
    if "node_conflicts" in payload:
        legacy = tuple(
            NodeMergeConflict.model_validate(c) for c in payload.pop("node_conflicts")
        )
        by_id = {c.node_id: c for c in legacy}
        if len(by_id) != len(legacy):
            raise ValueError("duplicate conflict sidecar owners")
        inline = {node.id: node for node in graph.nodes if node.alternatives}
        if (
            inline
            and {key: node.as_conflict() for key, node in inline.items()} != by_id
        ):
            raise ValueError("inline conflicts and node_conflicts sidecar disagree")
        marked = {
            node.id for node in graph.nodes if node.merge_status == MergeStatus.CONFLICT
        }
        if marked != set(by_id):
            raise ValueError(
                "conflict metadata and node merge_status must correspond exactly"
            )
        graph = _attach_conflicts(graph, by_id)
    _validate_inline_contract(graph, require_sources=False)
    payload["graph"] = graph
    return payload


def _attach_conflicts(
    graph: GraphSpace, by_id: dict[str, NodeMergeConflict | GraphNode]
) -> GraphSpace:
    if not by_id.keys() <= {node.id for node in graph.nodes}:
        raise ValueError("conflict references unknown owner node")
    nodes = []
    for node in graph.nodes:
        conflict = by_id.get(node.id)
        if conflict is None:
            nodes.append(
                node.without_conflict()
                if node.merge_status == MergeStatus.CONFLICT
                else node
            )
        elif isinstance(conflict, GraphNode):
            if conflict.merge_status != MergeStatus.CONFLICT:
                raise ValueError("with_conflicts requires conflicted owner nodes")
            nodes.append(conflict)
        else:
            nodes.append(node.with_conflict(conflict))
    return graph.model_copy(update={"nodes": tuple(nodes)})


def with_conflicts(
    graph: GraphSpace, conflicts: Iterable[NodeMergeConflict | GraphNode]
) -> GraphState:
    """Attach an explicit complete conflict set at a proposal/legacy boundary.

    A GraphNode replaces its full owner definition; a legacy DTO attaches only
    metadata. Existing conflict metadata absent from this set is cleared. This
    helper never changes caller objects and never stores a second representation.
    """
    values = tuple(conflicts)
    by_id = {c.id if isinstance(c, GraphNode) else c.node_id: c for c in values}
    if len(by_id) != len(values):
        raise ValueError("duplicate conflict owners")
    return GraphState(graph=_attach_conflicts(graph, by_id))


def _validate_inline_contract(graph: GraphSpace, *, require_sources: bool) -> None:
    placeholders = {"description": None, "aliases": [], "tags": []}
    for node in graph.nodes:
        if node.merge_status != MergeStatus.CONFLICT:
            if (
                node.common_sources
                or node.consolidation_status
                or node.preservation_reasons
                or node.alternatives
                or node.conflict_points
            ):
                raise ValueError("normal nodes cannot contain conflict metadata")
            continue
        if node.consolidation_status is None:
            raise ValueError("conflict node requires consolidation_status")
        conflict = node.as_conflict()
        if require_sources and (
            not node.sources or any(not a.sources for a in node.alternatives)
        ):
            raise ValueError(
                "inline conflict node and every alternative require nonempty sources"
            )
        expected_sources = set(node.common_sources) | {
            source for alt in node.alternatives for source in alt.sources
        }
        if set(node.sources) != expected_sources or len(set(node.sources)) != len(
            node.sources
        ):
            raise ValueError(
                "conflict node sources must equal the common and alternatives sources union"
            )
        for path in conflict.alternatives[0].overrides:
            if path.startswith("properties."):
                if path.split(".")[1] in node.properties:
                    raise ValueError(
                        f"variable field {path!r} also appears on the common node"
                    )
            elif getattr(node, path) != (
                node.id if path == "name" else placeholders[path]
            ):
                raise ValueError(
                    f"variable field {path!r} requires its common-node placeholder"
                )
        for alternative in node.alternatives:
            try:
                alternative_content(node, alternative)
            except ValidationError as exc:
                raise ValidationError.from_exception_data(
                    "GraphState",
                    [
                        {
                            **error,
                            "loc": (
                                "graph",
                                "nodes",
                                node.id,
                                "alternatives",
                                alternative.alternative_id,
                                *error["loc"],
                            ),
                        }
                        for error in exc.errors(include_url=False)
                    ],
                ) from exc


def export_inline_graph(state: GraphState) -> dict[str, Any]:
    """Serialize the stored graph directly, requiring real candidate provenance."""
    _validate_inline_contract(state.graph, require_sources=True)
    return state.graph.model_dump(mode="json")
