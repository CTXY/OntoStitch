"""Assessment scope comes from the compiled graph, never the author's change log."""

from cortex.capabilities.graph_merge.graph.publication import mapped_base
from cortex.capabilities.graph_merge.inputs import (
    PHYSICAL,
    semantic_relations_first,
)
from cortex.capabilities.graph_merge.integration.scope import active_contracts


def assessment_view(context, decision, preview, *, adjacency_limit=60):
    before_nodes, before_edges, _, _ = mapped_base(context, decision)
    nodes = {n.id: n for n in preview.state.graph.nodes}
    edges = {e.id: e for e in preview.state.graph.edges}

    def delta(before, after):
        return {
            "created": sorted(after.keys() - before.keys()),
            "updated": sorted(
                k for k in before.keys() & after.keys() if before[k] != after[k]
            ),
            "deleted": sorted(before.keys() - after.keys()),
        }

    node_delta, edge_delta = delta(before_nodes, nodes), delta(before_edges, edges)
    changed_edges = set(edge_delta["created"] + edge_delta["updated"])
    selected = set(node_delta["created"] + node_delta["updated"])
    # Retained incoming and previously inspected knowledge still needs review.
    selected.update(preview.node_map.get(key, key) for key in context.read_ids)
    for key in changed_edges | set(edge_delta["deleted"]):
        for edge in (before_edges.get(key), edges.get(key)):
            if edge is not None:
                selected.update((edge.from_node_id, edge.to_node_id))
    selected = {
        key for key in selected if key in nodes and nodes[key].node_type not in PHYSICAL
    }
    adjacent = [
        e
        for e in semantic_relations_first(edges.values(), nodes)
        if e.id not in changed_edges
        and (e.from_node_id in selected or e.to_node_id in selected)
    ]
    # All actual edge writes are visible, even beyond the ordinary adjacency page.
    relations = [edges[key] for key in sorted(changed_edges)] + adjacent[
        :adjacency_limit
    ]
    old_contracts = {c.id: c for c in context.contracts}
    return {
        "rationale_review_ids": sorted(
            k for k in selected if nodes[k].node_type == "generation_rule"
        ),
        "active_contracts": active_contracts(context, preview.state),
        "actual_changes": {"nodes": node_delta, "relations": edge_delta},
        "deleted_nodes": [
            before_nodes[key].model_dump(mode="json") for key in node_delta["deleted"]
        ],
        "deleted_relations": [
            before_edges[key].model_dump(mode="json") for key in edge_delta["deleted"]
        ],
        "changed_relation_contracts": [
            c.model_dump(mode="json")
            for c in preview.state.graph.relation_contracts
            if old_contracts.get(c.id) != c
        ],
        "candidate_nodes": [
            nodes[key].model_dump(mode="json") for key in sorted(selected)
        ],
        "candidate_relations": [e.model_dump(mode="json") for e in relations],
        "candidate_relations_total": len(changed_edges) + len(adjacent),
        "more_candidate_relations": len(adjacent) > adjacency_limit,
    }
