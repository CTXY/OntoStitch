from __future__ import annotations

from dataclasses import dataclass

from cortex.schema import GraphSpace, GraphState
from cortex.schema.relation_metadata import filter_metadata

PHYSICAL_TYPES = frozenset({'table', 'column', 'dimension_value'})
SEMANTIC_TYPES = frozenset({'concept', 'metric', 'generation_rule', 'event'})


@dataclass
class ContextSelection:
    node_ids: set[str]
    seed_ids: set[str]
    skipped: dict[str, dict]
    semantic_nodes: int
    physical_nodes: int


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


def _required_context(state, node_ids):
    """Complete references, full alternatives and induced-edge prerequisites."""
    nodes = {node.id: node for node in state.graph.nodes}
    result = set(node_ids) & nodes.keys()
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
        # Common relation groups on a conflict belong to every complete branch.
        required_edge_ids.update(edge_id for ref in result if nodes[ref].alternatives
            for group in nodes[ref].properties.get('relation_metadata', ())
            for edge_id in group['edges'])
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


def select_context(state: GraphState, seed_node_ids: set[str], *, ranked_seed_ids=None,
                   excluded_node_ids=(), semantic_node_limit=30, physical_node_limit=50) -> ContextSelection:
    """Admit complete knowledge units under final, independent hard node caps.

    Ranked seeds precede optional one-hop neighbors and physical JOIN routing.
    Every admission includes the full mandatory reference closure, including
    effects of edges induced with already admitted nodes. An indivisible unit
    that does not fit is skipped whole; dependencies never bypass the caps.
    """
    if any(type(n) is not int or n < 1 for n in (semantic_node_limit, physical_node_limit)):
        raise ValueError('Final semantic/physical node limits must be positive integers')
    nodes = {n.id: n for n in state.graph.nodes}
    if any(n.node_type not in SEMANTIC_TYPES | PHYSICAL_TYPES for n in nodes.values()):
        raise ValueError('Unsupported node type in bounded retrieval context')
    seeds = set(seed_node_ids) & nodes.keys()
    excluded = set(excluded_node_ids) - seeds
    order = list(dict.fromkeys(ref for ref in (ranked_seed_ids or ()) if ref in seeds))
    order.extend(sorted(seeds - set(order)))
    result, skipped = set(), {}

    def counts(refs):
        physical = sum(nodes[ref].node_type in PHYSICAL_TYPES for ref in refs)
        return len(refs) - physical, physical

    def admit(ref, phase):
        nonlocal result
        if ref in result:
            return True
        candidate = _required_context(state, result | {ref})
        semantic, physical = counts(candidate)
        if semantic > semantic_node_limit or physical > physical_node_limit:
            skipped[ref] = {'phase': phase, 'reason': 'complete_unit_exceeds_final_node_budget',
                'candidate_semantic_nodes': semantic, 'candidate_physical_nodes': physical}
            return False
        result = candidate
        return True

    for ref in order:
        admit(ref, 'seed')
    # Only admitted original seeds are business traversal roots. A new neighbor
    # or mandatory reference cannot start another business hop.
    roots = seeds & result
    neighbors = {}
    root_rank = {ref: rank for rank, ref in enumerate(order)}
    for edge in state.graph.edges:
        for root, other in ((edge.from_node_id, edge.to_node_id), (edge.to_node_id, edge.from_node_id)):
            if root in roots and other in nodes and other not in excluded and other not in result:
                neighbors[other] = min(neighbors.get(other, len(order)), root_rank[root])
    for ref in sorted(neighbors, key=lambda ref: (neighbors[ref], ref)):
        admit(ref, 'one_hop')
    # Preserve the existing at-most-two-hop physical routing opportunity, now
    # charged to the same final budget, including complete column_pairs.
    frontier = {ref for ref in result if nodes[ref].node_type == 'table'}
    for _ in range(2):
        neighbors = {endpoint for edge in state.graph.edges if edge.edge_type == 'joins_with'
            and (edge.from_node_id in frontier or edge.to_node_id in frontier)
            for endpoint in (edge.from_node_id, edge.to_node_id)
            if endpoint in nodes and nodes[endpoint].node_type == 'table' and endpoint not in result}
        added = {ref for ref in sorted(neighbors) if admit(ref, 'join_route')}
        if not added:
            break
        frontier = added
    semantic, physical = counts(result)
    assert semantic <= semantic_node_limit and physical <= physical_node_limit
    return ContextSelection(result, seeds & result, skipped, semantic, physical)


def expand_context(state: GraphState, seed_node_ids: set[str], *, excluded_node_ids=(),
                   semantic_node_limit=30, physical_node_limit=50) -> set[str]:
    """One asserted business hop with complete references and final hard caps."""
    return select_context(state, seed_node_ids, excluded_node_ids=excluded_node_ids,
        semantic_node_limit=semantic_node_limit, physical_node_limit=physical_node_limit).node_ids


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
    "select_context",
    "without_untrusted_dimension_values",
]
