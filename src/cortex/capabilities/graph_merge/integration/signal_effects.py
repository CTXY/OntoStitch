"""Necessary semantic impact of actual edits, separate from signal selection."""
from cortex.capabilities.graph_merge.integration.context import references
from cortex.capabilities.graph_merge.integration.task_context import (
    _contract_affected_edges,
    _metadata_edges,
    semantic_review_targets,
)


def modification_scope(before, candidate):
    old_nodes = {n.id: n for n in before.graph.nodes}
    new_nodes = {n.id: n for n in candidate.graph.nodes}
    old_edges = {e.id: e for e in before.graph.edges}
    new_edges = {e.id: e for e in candidate.graph.edges}
    changed_nodes = {key for key in old_nodes.keys() | new_nodes.keys()
                     if old_nodes.get(key) != new_nodes.get(key)}
    changed_edges = {key for key in old_edges.keys() | new_edges.keys()
                     if old_edges.get(key) != new_edges.get(key)}
    def meaning(node):
        if node is None:
            return None
        row = node.model_dump(mode='json', exclude={'sources', 'reference_origin'})
        row['properties'].pop('relation_metadata', None)
        return row
    changed_meanings = {key for key in changed_nodes
                        if meaning(old_nodes.get(key)) != meaning(new_nodes.get(key))}
    selected_nodes = set(changed_nodes)
    selected_edges = changed_edges | _contract_affected_edges(before, candidate)
    consumers = set(semantic_review_targets(before, candidate, ())['targets'])
    for nodes, edges in ((old_nodes, old_edges), (new_nodes, new_edges)):
        # A changed endpoint does not turn every incident edge into a premise.
        # Direct consumers and complete affected metadata groups do matter.
        for node in nodes.values():
            metadata = _metadata_edges(node)
            if metadata & changed_edges:
                consumers.add(node.id)
                selected_edges.update(metadata)
        selected_nodes.update(consumers)
        for ident in consumers | changed_nodes:
            node = nodes.get(ident)
            if node is None:
                continue
            selected_nodes.update(references(node))
            if node.node_type == 'metric':
                selected_edges.update(e.id for e in edges.values()
                                      if e.from_node_id == ident and e.edge_type in {'depends_on', 'grounded_on'})
        for edge in edges.values():
            if edge.from_node_id in changed_meanings or edge.to_node_id in changed_meanings:
                selected_edges.add(edge.id)
            if edge.edge_type in {'depends_on', 'applies_to'} and edge.to_node_id in changed_nodes:
                selected_edges.add(edge.id)
            if edge.id in selected_edges:
                selected_nodes.update((edge.from_node_id, edge.to_node_id))
        # A changed binding must expose all actual competing owners of that
        # exact physical target, not the claimants' unrelated relationships.
        physical = {edges[key].to_node_id for key in changed_edges & edges.keys()
                    if edges[key].edge_type == 'grounded_on'}
        for edge in edges.values():
            if edge.edge_type == 'grounded_on' and edge.to_node_id in physical:
                selected_edges.add(edge.id)
                selected_nodes.update((edge.from_node_id, edge.to_node_id))
    return selected_nodes, selected_edges
