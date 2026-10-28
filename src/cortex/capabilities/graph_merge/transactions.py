"""Compile reviewed merge snapshots into the ordinary atomic graph boundary."""

from __future__ import annotations

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.schema import GraphState


@traceable(name="merge_transaction", run_type="chain")
def apply_merge_state(manager: GraphManager, state: GraphState, *, request_id: str):
    trace = get_current_run_tree()
    if trace is not None:
        trace.add_metadata(
            {"transaction_id": request_id, "base_revision": manager.revision}
        )
    before = manager.get_snapshot()
    old_nodes = {node.id: node for node in before.graph.nodes}
    new_nodes = {node.id: node for node in state.graph.nodes}
    old_conflicts = {conflict.id: conflict for conflict in before.conflict_nodes}
    new_conflicts = {conflict.id: conflict for conflict in state.conflict_nodes}
    operations = []
    for node in state.graph.nodes:
        conflict = new_conflicts.get(node.id)
        if node == old_nodes.get(node.id) and conflict == old_conflicts.get(node.id):
            continue
        operation = {
            "operation": "set_conflict"
            if conflict
            else "update_node"
            if node.id in old_nodes
            else "create_node",
            "node": node.model_dump(mode="json"),
        }
        operations.append(operation)
    for node_id in old_nodes.keys() - new_nodes.keys():
        operations.append({"operation": "delete_node", "node_id": node_id})
    old_edges = {
        edge.id: edge
        for edge in before.graph.edges
        if edge.reference_origin != "default"
    }
    new_edges = {
        edge.id: edge
        for edge in state.graph.edges
        if edge.reference_origin != "default"
    }
    for edge_id in old_edges.keys() - new_edges.keys():
        operations.append({"operation": "delete_edge", "edge_id": edge_id})
    for edge_id, edge in new_edges.items():
        if edge != old_edges.get(edge_id):
            operations.append(
                {
                    "operation": "update_edge"
                    if edge_id in old_edges
                    else "create_edge",
                    "edge": edge.model_dump(mode="json"),
                }
            )
    changed_owners = {
        operation["node"]["id"] for operation in operations if "node" in operation
    }
    for edge_id in old_edges.keys() | new_edges.keys():
        if old_edges.get(edge_id) == new_edges.get(edge_id):
            continue
        edge = new_edges.get(edge_id) or old_edges[edge_id]
        owner = edge.from_node_id
        if owner in new_conflicts and owner not in changed_owners:
            operations.append(
                {
                    "operation": "set_conflict",
                    "node": new_nodes[owner].model_dump(mode="json"),
                }
            )
            changed_owners.add(owner)
    if not operations:
        manager.validate_state(state)
        return before
    result = manager.apply(
        operations,
        expected_revision=manager.revision,
        request_id=request_id,
        reviewed_reconciliation=True,
    )
    if trace is not None:
        trace.add_metadata(
            {"transaction_status": result.status, "result_revision": result.revision}
        )
    if result.status != "applied":
        raise GraphRejected(result.errors, result.revision)
    return manager.get_snapshot()


def preview_group(context, reconciled):
    from cortex.capabilities.graph_merge.edges import normalize_edges
    from cortex.schema import GraphSpace
    from cortex.schema.references import remap_references

    existing = context.existing_state
    if existing is None:
        return None
    node = reconciled.node.model_copy(
        update={
            "properties": remap_references(
                reconciled.node.properties, context.node_mappings
            )
        }
    )
    if reconciled.conflict is not None:
        node = node.with_conflict(reconciled.conflict)
    nodes = tuple(n for n in existing.graph.nodes if n.id != node.id) + (node,)
    node_ids = {n.id for n in nodes}
    member_ids = {m.ref.ref_id for m in context.group.members}
    incoming_edges = tuple(
        edge
        for edge in context.input_relations
        if edge.from_node_id in member_ids
        and context.node_mappings.get(edge.to_node_id, edge.to_node_id) in node_ids
    )
    normalized = normalize_edges(
        existing=existing.graph.edges,
        incoming=incoming_edges,
        nodes=nodes,
        node_mappings=context.node_mappings,
        schema=context.schema_definition,
    )
    candidate = GraphState(graph=GraphSpace(nodes=nodes, edges=normalized.edges))
    manager = GraphManager(context.schema_definition, existing)
    return apply_merge_state(
        manager, candidate, request_id=f"preview:{context.group.group_id}"
    )
