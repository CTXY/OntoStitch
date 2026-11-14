from __future__ import annotations

from cortex.schema import GraphSpace, GraphState
from cortex.schema.relation_metadata import filter_metadata


def _metadata_view(node, edge_ids):
    # Branch-specific statements distinguish unresolved alternatives. Dropping
    # them can silently collapse that conflict; refuse a partial view instead.
    for alternative in node.alternatives:
        for group in (alternative.overrides.get("properties.relation_metadata") or []):
            if not set(group["edges"]) <= edge_ids:
                raise ValueError(
                    f"Cannot project conflict {node.id}: retain all edges of "
                    f"relation_metadata group {group['id']} in {alternative.alternative_id}"
                )
    return node.model_copy(update={"properties": filter_metadata(node.properties, edge_ids)})


def expand_context(state: GraphState, seed_node_ids: set[str]) -> set[str]:
    """One asserted hop, followed only by mandatory references and JOIN routing.

    Neighbours do not become new business traversal roots. In particular a
    physical column reached from a Concept must not fan back out to every other
    Concept sharing that column. Predicates need no hard-coded traversal list.
    """

    nodes = {node.id: node for node in state.graph.nodes}
    seeds = seed_node_ids & nodes.keys()
    result = set(seeds)
    for edge in state.graph.edges:
        if edge.from_node_id in seeds:
            result.add(edge.to_node_id)
        if edge.to_node_id in seeds:
            result.add(edge.from_node_id)
    result.intersection_update(nodes)
    # Physical joins are Table-to-Table edges. Include up to two hops so a
    # semantic source pair can retain one intermediate routing table.
    table_frontier = {
        node_id for node_id in result if nodes[node_id].node_type == "table"
    }
    for _depth in range(2):
        neighbors = {
            endpoint
            for edge in state.graph.edges
            if edge.edge_type == "joins_with"
            and (
                edge.from_node_id in table_frontier or edge.to_node_id in table_frontier
            )
            for endpoint in (edge.from_node_id, edge.to_node_id)
            if endpoint in nodes and nodes[endpoint].node_type == "table"
        } - result
        if len(result) + len(neighbors) > 160:
            break
        result.update(neighbors)
        table_frontier = neighbors
        if not table_frontier:
            break
    # Mandatory references have no heuristic depth limit: preserve complete formulas,
    # alternatives, value ownership, and composite JOIN endpoints.
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema.references import property_reference_ids

    conflicts = {item.id: item for item in state.conflict_nodes}
    while True:
        required = set()
        for ref in result:
            node = nodes[ref]
            required.update(GraphManager._node_references(node, nodes))
            if ref in conflicts:
                for alternative in conflicts[ref].alternatives:
                    required.update(
                        GraphManager._node_references(
                            node,
                            nodes,
                            GraphManager.resolve_alternative(
                                node, alternative
                            ).properties,
                        )
                    )
        required_edge_ids = {
            edge_id
            for ref in result
            for alternative in nodes[ref].alternatives
            for group in alternative.overrides.get("properties.relation_metadata", ())
            for edge_id in group["edges"]
        }
        for edge in state.graph.edges:
            if edge.id in required_edge_ids:
                required.update((edge.from_node_id, edge.to_node_id))
            if edge.from_node_id in result and edge.to_node_id in result:
                required.update(property_reference_ids(edge.properties))
                for pair in edge.properties.get("column_pairs", ()):
                    required.update(
                        pair[key]
                        for key in ("from_column_ref", "to_column_ref")
                        if key in pair
                    )
        added = (required & nodes.keys()) - result
        if not added:
            break
        result.update(added)
    return result


def build_retrieved_graph_state(state: GraphState, node_ids: set[str]) -> GraphState:
    """Build a task-local state with complete inline conflict nodes."""

    nodes = tuple(node for node in state.graph.nodes if node.id in node_ids)
    retained = {node.id for node in nodes}
    edges = tuple(
        edge
        for edge in state.graph.edges
        if edge.from_node_id in retained and edge.to_node_id in retained
    )
    nodes = tuple(_metadata_view(node, {e.id for e in edges}) for node in nodes)
    return GraphState(graph=state.graph.model_copy(update={"nodes": nodes, "edges": edges}))


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
    retained_edges = tuple(edge for edge in state.graph.edges
                           if edge.from_node_id not in blocked and edge.to_node_id not in blocked)
    graph = GraphSpace(
        relation_contracts=state.graph.relation_contracts,
        nodes=tuple(_metadata_view(node, {e.id for e in retained_edges}) for node in state.graph.nodes if node.id not in blocked),
        edges=retained_edges,
    )
    return GraphState(
        graph=graph,
    )


__all__ = [
    "build_retrieved_graph_state",
    "expand_context",
    "without_untrusted_dimension_values",
]
