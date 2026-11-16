"""Task-local Rule applicability packets, built after user-choice projection."""
from .projection import guidance_selection_refs, task_asset_payload


def build_rule_context(graph, relevant_ids, choice_points, selected_by_point):
    confirmed, unselected = guidance_selection_refs(choice_points, selected_by_point)
    eligible = (set(relevant_ids) | confirmed) - (unselected - confirmed)
    nodes = {node.id: node for node in graph.nodes}
    packets = []
    for node in graph.nodes:
        if node.id not in eligible or node.node_type != "generation_rule":
            continue
        # Unresolved branches are not simultaneously applicable rules. Only
        # safely projected normal definitions may become downstream instructions.
        if node.alternatives:
            continue
        asset = task_asset_payload(node, nodes_by_id=nodes, relations=graph.edges)
        related = []
        for edge in graph.edges:
            if node.id not in (edge.from_node_id, edge.to_node_id):
                continue
            other_id = edge.to_node_id if edge.from_node_id == node.id else edge.from_node_id
            other = nodes[other_id]
            related.append({
                "relation": edge.edge_type,
                "direction": "outgoing" if edge.from_node_id == node.id else "incoming",
                "description": edge.properties.get("description", ""),
                "object": task_asset_payload(other, nodes_by_id=nodes, relations=graph.edges)
                if other.node_type in {"concept", "metric", "event"}
                else {"name": other.name, "meaning": other.description},
            })
        packets.append({
            "rule_id": node.id, "name": node.name,
            **{k: asset.get(k, "") for k in ("condition", "action", "reason", "reference_bindings")},
            "operation_intents": node.properties.get("operation_intents", []),
            "related_objects": related,
        })
    return tuple(packets)
