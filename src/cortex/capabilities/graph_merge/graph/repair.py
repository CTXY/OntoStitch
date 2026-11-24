"""Locate controller-reported contract errors without choosing semantic repairs."""

from cortex.capabilities.graph_merge.inputs import PHYSICAL


def validation_targets(state, issues):
    nodes = {n.id: n for n in state.graph.nodes}
    edges = {e.id: e for e in state.graph.edges}
    known = nodes.keys() | edges.keys()
    targets = set()

    def collect(value):
        if isinstance(value, str) and value in known:
            targets.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    for issue in issues:
        collect(issue.details)
        for prefix, objects in (("nodes.", nodes), ("edges.", edges)):
            if issue.path.startswith(prefix):
                path = issue.path[len(prefix):]
                matches = [k for k in objects if path == k or path.startswith(k + ".")]
                if matches:
                    targets.add(max(matches, key=len))
    # Include the participants in an invalid assertion and all competing direct
    # physical bindings. The Agent still decides ownership/identity/disposition.
    for key in tuple(targets):
        if key in edges:
            targets.update((edges[key].from_node_id, edges[key].to_node_id))
    physical = {k for k in targets if k in nodes and nodes[k].node_type in PHYSICAL}
    for edge in edges.values():
        if edge.edge_type == "grounded_on" and edge.to_node_id in physical:
            targets.update((edge.id, edge.from_node_id))
    return targets
