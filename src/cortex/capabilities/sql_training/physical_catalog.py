"""Canonical physical IDs for database-scoped training graphs."""

from __future__ import annotations

from cortex.capabilities.graph_merge.graph.operations import PHYSICAL, remap_edge, remap_node
from cortex.capabilities.sql_training.core.ids import make_node_id
from cortex.schema import GraphNode, GraphSpace, GraphState


def physical_id_mapping(graph: GraphSpace) -> dict[str, str]:
    """Resolve owners before assigning IDs, independent of source ID spelling."""
    nodes = {node.id: node for node in graph.nodes}
    mapping, identities = {}, {}
    for kind in ("table", "column", "dimension_value"):
        for node in graph.nodes:
            if node.node_type != kind:
                continue
            props = node.properties
            if kind == "table":
                parts = (props["table_name"],)
                identity = (props.get("database"), *parts)
            elif kind == "column":
                table = nodes.get(props["table_ref"])
                if table is None or table.node_type != "table":
                    raise ValueError(f"Missing table owner for {node.id}")
                parts = (table.properties["table_name"], props["column_name"])
                identity = (mapping[table.id], parts[1])
            else:
                column = nodes.get(props["column_ref"])
                if column is None or column.node_type != "column":
                    raise ValueError(f"Missing column owner for {node.id}")
                table = nodes[column.properties["table_ref"]]
                parts = (
                    table.properties["table_name"],
                    column.properties["column_name"],
                    props["value"],
                )
                # Keep value types distinct even when their string encodings coincide.
                identity = (
                    mapping[column.id],
                    type(props["value"]).__name__,
                    props["value"],
                )
            canonical = make_node_id(kind, *parts)
            if canonical in identities and identities[canonical] != identity:
                raise ValueError(f"Physical ID collision at {canonical}")
            if canonical in nodes and nodes[canonical].node_type not in PHYSICAL:
                raise ValueError(f"Physical ID collides with semantic node {canonical}")
            identities[canonical] = identity
            mapping[node.id] = canonical
    return mapping


def merge_physical_node(previous: GraphNode, incoming: GraphNode) -> GraphNode:
    """Keep physical facts fixed while combining independent source annotations."""
    left = previous.model_dump(exclude={"sources"})
    right = incoming.model_dump(exclude={"sources"})
    # Accepted case-specific synonyms are annotations, not different physical
    # identities. Keep them all; column ownership, value, type and other facts
    # still have to match exactly.
    left.pop("aliases")
    right.pop("aliases")
    old_metadata = left["properties"].pop("source_metadata", {}) or {}
    new_metadata = right["properties"].pop("source_metadata", {}) or {}
    if left != right:
        raise ValueError(f"Conflicting physical catalog content at {incoming.id}")
    metadata = {source: dict(fields) for source, fields in old_metadata.items()}
    for source, fields in new_metadata.items():
        combined = metadata.setdefault(source, {})
        for key, value in fields.items():
            if key in combined and combined[key] != value:
                raise ValueError(
                    f"Conflicting catalog metadata at {incoming.id}: {source}.{key}"
                )
            combined[key] = value
    properties = dict(previous.properties)
    if metadata:
        properties["source_metadata"] = metadata
    return previous.model_copy(
        update={
            "properties": properties,
            "aliases": list(dict.fromkeys((*previous.aliases, *incoming.aliases))),
            "sources": tuple(dict.fromkeys((*previous.sources, *incoming.sources))),
        }
    )


def canonicalize_physical_graph(state: GraphState) -> tuple[GraphState, dict[str, str]]:
    """Return a renamed copy and explicit mapping, preserving semantic content."""
    mapping = physical_id_mapping(state.graph)
    nodes = {}
    for node in state.graph.nodes:
        renamed = remap_node(node, mapping)
        if renamed.id in nodes:
            renamed = merge_physical_node(nodes[renamed.id], renamed)
        nodes[renamed.id] = renamed
    graph = state.graph.model_copy(
        update={
            "nodes": tuple(nodes.values()),
            "edges": tuple(remap_edge(edge, mapping) for edge in state.graph.edges),
        }
    )
    return GraphState(graph=graph), mapping


def audit_physical_ids(state: GraphState) -> None:
    wrong = {
        old: new for old, new in physical_id_mapping(state.graph).items() if old != new
    }
    if wrong:
        raise ValueError(f"Published graph has noncanonical physical IDs: {wrong}")


def combine_physical_catalogs(states) -> GraphSpace:
    """Collect canonical facts while leaving each input's semantic evidence isolated."""
    from cortex.capabilities.graph_merge.graph.operations import deduplicate_edges
    from cortex.schema.relation_contracts import merge_relation_contracts

    nodes, edges = {}, []
    contracts = ()
    for state in states:
        normalized, _ = canonicalize_physical_graph(state)
        contracts = merge_relation_contracts(
            contracts, normalized.graph.relation_contracts
        )
        for node in normalized.graph.nodes:
            if node.node_type in PHYSICAL:
                if node.id in nodes:
                    node = merge_physical_node(nodes[node.id], node)
                nodes[node.id] = node
        edges.extend(
            edge
            for edge in normalized.graph.edges
            if edge.from_node_id in nodes and edge.to_node_id in nodes
        )
    canonical_edges, edge_mapping = deduplicate_edges(edges)
    return GraphSpace(
        nodes=tuple(remap_node(node, {}, edge_mapping) for node in nodes.values()),
        edges=tuple(canonical_edges.values()),
        relation_contracts=contracts,
    )
