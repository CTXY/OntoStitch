from __future__ import annotations

from cortex.schema import GraphSpace, GraphState


def expand_context(state: GraphState, seed_node_ids: set[str]) -> set[str]:
    """Return a bounded two-hop semantic neighborhood around selected retrieval seeds."""

    result = set(seed_node_ids)
    nodes = {node.id: node for node in state.graph.nodes}
    frontier = set(seed_node_ids)
    for _depth in range(2):
        next_frontier: set[str] = set()
        for edge in state.graph.edges:
            if edge.from_node_id in frontier:
                next_frontier.add(edge.to_node_id)
            if edge.to_node_id in frontier and edge.edge_type in {
                "maps_to_column",
                "maps_to_value",
                "applies_to",
                "grounded_on",
            }:
                next_frontier.add(edge.from_node_id)
        # Values only enter through their own recall or an explicit semantic binding.
        next_frontier = {ref for ref in next_frontier if ref in nodes}
        next_frontier -= result
        result.update(next_frontier)
        frontier = next_frontier
        if not frontier or len(result) >= 160:
            break
    # Physical joins are Table-to-Table edges. Include up to two hops so a
    # semantic source pair can retain one intermediate routing table.
    table_frontier = {node_id for node_id in result if nodes[node_id].node_type == "table"}
    for _depth in range(2):
        neighbors = {
            endpoint
            for edge in state.graph.edges
            if edge.edge_type == "joins_with"
            and (edge.from_node_id in table_frontier or edge.to_node_id in table_frontier)
            for endpoint in (edge.from_node_id, edge.to_node_id)
            if endpoint in nodes and nodes[endpoint].node_type == "table"
        } - result
        if len(result) + len(neighbors) > 160:
            break
        result.update(neighbors)
        table_frontier = neighbors
        if not table_frontier:
            break
    # Never drop selected/exact seeds by alphabetic truncation.
    return result


def build_retrieved_graph_state(state: GraphState, node_ids: set[str]) -> GraphState:
    """Build a task-local GraphState while retaining complete conflict overlays."""

    nodes = tuple(node for node in state.graph.nodes if node.id in node_ids)
    retained = {node.id for node in nodes}
    edges = tuple(
        edge
        for edge in state.graph.edges
        if edge.from_node_id in retained and edge.to_node_id in retained
    )
    conflicts = tuple(item for item in state.node_conflicts if item.node_id in retained)
    return GraphState(graph=GraphSpace(nodes=nodes, edges=edges), node_conflicts=conflicts)


def without_untrusted_dimension_values(state: GraphState) -> GraphState:
    blocked = {
        node.id
        for node in state.graph.nodes
        if node.node_type == "dimension_value"
        and not any(
            (source.locator or "").casefold() == "database"
            # Imported database-schema values use an explicit schema provenance namespace.
            # An arbitrary inferred value merely labelled database_schema is not trusted.
            or (
                (source.locator or "").casefold() == "database_schema"
                and source.source_id.startswith("schema:")
            )
            for source in node.sources
        )
    }
    if not blocked:
        return state
    graph = GraphSpace(
        nodes=tuple(node for node in state.graph.nodes if node.id not in blocked),
        edges=tuple(
            edge
            for edge in state.graph.edges
            if edge.from_node_id not in blocked and edge.to_node_id not in blocked
        ),
    )
    return GraphState(
        graph=graph,
        node_conflicts=tuple(item for item in state.node_conflicts if item.node_id not in blocked),
    )


__all__ = [
    "build_retrieved_graph_state",
    "expand_context",
    "without_untrusted_dimension_values",
]
