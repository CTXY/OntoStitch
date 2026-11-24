"""Local transactions may retain an input binding contest, never broaden it.

A validation-only copy isolates existing contests so they cannot mask another
error. It never changes the returned graph. Publication remains fully strict.
"""

from collections import defaultdict

from cortex.application.ontology_graph import GraphManager
from cortex.schema.relation_contracts import effective_schema
from cortex.schema.validation import validate_graph_state


def _owners(state):
    kinds = {n.id: n.node_type for n in state.graph.nodes}
    result = defaultdict(set)
    for edge in state.graph.edges:
        if (
            edge.edge_type == "grounded_on"
            and kinds.get(edge.from_node_id) == "concept"
        ):
            result[edge.to_node_id].add(edge.from_node_id)
    return result


def validate_local_transition(schema, before, after):
    validate_graph_state(after, effective_schema(schema, after.graph))
    old, new = _owners(before), _owners(after)
    contests = {physical: owners for physical, owners in new.items() if len(owners) > 1}
    if not contests:
        return GraphManager(schema).validate_state(after)
    if any(
        not owners <= old.get(physical, set()) for physical, owners in contests.items()
    ):
        # Return the ordinary detailed graph diagnostics for a new violation.
        return GraphManager(schema).validate_state(after)
    selected = {physical: min(owners) for physical, owners in contests.items()}
    edges = tuple(
        edge
        for edge in after.graph.edges
        if not (
            edge.edge_type == "grounded_on"
            and edge.to_node_id in selected
            and edge.from_node_id in contests[edge.to_node_id]
            and edge.from_node_id != selected[edge.to_node_id]
        )
    )
    probe = after.model_copy(
        update={"graph": after.graph.model_copy(update={"edges": edges})}
    )
    GraphManager(schema).validate_state(probe)
    return after
