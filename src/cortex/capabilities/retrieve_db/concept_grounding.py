from __future__ import annotations

from cortex.schema.graph import GraphEdge, GraphSpace


def effective_concept_grounding(
    graph: GraphSpace, concept_id: str
) -> tuple[GraphEdge, ...]:
    """Return direct and uniquely inherited grounding edges for one Concept."""

    node_by_id = {node.id: node for node in graph.nodes}
    node = node_by_id.get(concept_id)
    if node is None or node.node_type != "concept":
        raise ValueError(f"unknown Concept node {concept_id!r}")

    result: dict[str, GraphEdge] = {}
    visited: set[str] = set()
    current_id = concept_id
    while True:
        if current_id in visited:
            raise ValueError("broader_than hierarchy contains a cycle")
        visited.add(current_id)
        for edge in graph.edges:
            if edge.from_node_id == current_id and (
                edge.edge_type == "grounded_on" or edge.reference_origin is not None
            ):
                result[edge.id] = edge
        parents = {
            edge.from_node_id
            for edge in graph.edges
            if edge.edge_type == "broader_than" and edge.to_node_id == current_id
        }
        if not parents:
            break
        if len(parents) != 1:
            raise ValueError("effective grounding requires a unique direct parent")
        current_id = next(iter(parents))
    return tuple(sorted(result.values(), key=lambda edge: (edge.edge_type, edge.id)))
