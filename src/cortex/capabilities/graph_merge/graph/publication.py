"""Publish one accepted graph with stable public IDs and exact provenance mappings."""

from __future__ import annotations

from dataclasses import dataclass

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from cortex.application.merge_graph import (
    CanonicalIdMapping,
    GraphMergeResult,
    MergeElementRef,
    StructuralRewrite,
)
from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.graph.operations import (
    PHYSICAL,
    remap_edge,
    remap_node,
)
from cortex.schema import GraphState


def assessed_semantic_content(state):
    """Compare effective assertions, preserving every partial branch scope."""
    nodes = {n.id: n for n in state.graph.nodes}
    assertions = {}
    for edge in state.graph.edges:
        if edge.reference_origin == "default":
            continue
        data = edge.model_dump(mode="json", exclude={"sources", "reference_origin"})
        scope = data["properties"].get("alternative_ids")
        alternatives = {a.alternative_id for a in nodes[edge.from_node_id].alternatives}
        # In this immutable snapshot, unscoped and explicitly all current
        # alternatives mean the same assertion. A subset is never erased.
        if scope is not None and (
            not scope or alternatives and set(scope) == alternatives
        ):
            data["properties"].pop("alternative_ids")
        assertions[edge.id] = data
    return (
        {
            n.id: n.model_dump(mode="json", exclude={"sources"})
            for n in state.graph.nodes
            if n.node_type not in PHYSICAL
        },
        assertions,
    )


@dataclass
class Preview:
    state: GraphState
    node_map: dict[str, str]
    edge_map: dict[str, str]
    impacts: list[dict]


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
    if (
        not operations
        and state.graph.relation_contracts == before.graph.relation_contracts
    ):
        manager.validate_state(state)
        return before
    result = manager.apply(
        operations,
        expected_revision=manager.revision,
        request_id=request_id,
        reviewed_reconciliation=True,
        relation_contracts=state.graph.relation_contracts,
    )
    if trace is not None:
        trace.add_metadata(
            {"transaction_status": result.status, "result_revision": result.revision}
        )
    if result.status != "applied":
        raise GraphRejected(result.errors, result.revision)
    return manager.get_snapshot()


def commit_result(context, proposal, preview, *, run_id):
    # Short handles reduce model noise; they are never the final source identity.
    def publish_ids(items, existing_ids, input_map):
        inverse = {value: key for key, value in input_map.items()}
        occupied = set(existing_ids) | {
            item.id for item in items if item.id not in inverse
        }
        result = {}
        for item in items:
            if item.id not in inverse or item.id in existing_ids:
                continue
            public = inverse[item.id]
            while public in occupied:
                public = "merged::" + public
            occupied.add(public)
            result[item.id] = public
        return result

    final_nodes = publish_ids(
        preview.state.graph.nodes,
        {n.id for n in context.existing.graph.nodes},
        context.input_node_map,
    )
    final_edges = publish_ids(
        preview.state.graph.edges,
        {e.id for e in context.existing.graph.edges},
        context.input_edge_map,
    )
    published = preview.state.model_copy(
        update={
            "graph": preview.state.graph.model_copy(
                update={
                    "nodes": tuple(
                        remap_node(n, final_nodes, final_edges)
                        for n in preview.state.graph.nodes
                    ),
                    "edges": tuple(
                        remap_edge(e, final_nodes, final_edges)
                        for e in preview.state.graph.edges
                    ),
                }
            )
        }
    )
    node_aliases = {
        key: final_nodes.get(value, value) for key, value in preview.node_map.items()
    }
    node_aliases.update(
        {key: value for key, value in final_nodes.items() if key not in node_aliases}
    )
    edge_aliases = {
        key: final_edges.get(value, value) for key, value in preview.edge_map.items()
    }
    edge_aliases.update(
        {key: value for key, value in final_edges.items() if key not in edge_aliases}
    )
    manager = GraphManager(context.schema, context.existing)
    base_revision = manager.revision
    actual = apply_merge_state(manager, published, request_id=f"integration:{run_id}")
    # GraphManager may normalize default edges. It must not silently remove authored meaning.
    actual_nodes = {n.id for n in actual.graph.nodes}
    actual_edges = {e.id for e in actual.graph.edges}
    missing = {
        e.id for e in published.graph.edges if e.reference_origin != "default"
    } - actual_edges
    if missing:
        raise ValueError(
            f"Commit unexpectedly removed authored relations: {sorted(missing)}"
        )

    if assessed_semantic_content(actual) != assessed_semantic_content(published):
        raise ValueError(
            "Commit changed assessed semantic content; no result published"
        )
    # Catalog ownership is regenerated by GraphManager under public node IDs.
    # Recover its correspondence from endpoints, never use this shortcut for
    # authored business assertions whose scope/properties may differ.
    actual_node_index = {n.id: n for n in actual.graph.nodes}
    catalog_index = {}
    for edge in actual.graph.edges:
        # An authored ownership edge suppresses default generation for the same
        # catalog fact. It can be the destination, but only default source edges
        # enter this repair below; authored joins/business edges are not remapped.
        if all(
            actual_node_index[key].node_type in PHYSICAL
            for key in (edge.from_node_id, edge.to_node_id)
        ):
            signature = (edge.edge_type, edge.from_node_id, edge.to_node_id)
            catalog_index.setdefault(signature, []).append(edge.id)
    catalog_aliases = {}
    for edge in context.raw_edges.values():
        if edge.reference_origin != "default" or not all(
            context.raw_nodes[key].node_type in PHYSICAL
            for key in (edge.from_node_id, edge.to_node_id)
        ):
            continue
        signature = (
            edge.edge_type,
            node_aliases.get(edge.from_node_id, edge.from_node_id),
            node_aliases.get(edge.to_node_id, edge.to_node_id),
        )
        matches = catalog_index.get(signature, ())
        if len(matches) != 1:
            raise ValueError(
                f"Expected one committed catalog edge for {edge.id}; found {len(matches)}"
            )
        catalog_aliases[edge.id] = matches[0]
        catalog_aliases[edge_aliases.get(edge.id, edge.id)] = matches[0]
    edge_aliases = {
        key: catalog_aliases.get(value, value) for key, value in edge_aliases.items()
    }
    edge_aliases.update(catalog_aliases)
    inverse_nodes = {v: k for k, v in context.input_node_map.items()}
    inverse_edges = {v: k for k, v in context.input_edge_map.items()}

    def reference(e, *, after):
        inverse = inverse_nodes if e.kind == "node" else inverse_edges
        mapping = node_aliases if e.kind == "node" else edge_aliases
        return MergeElementRef(
            kind=e.kind,
            id=mapping.get(e.id, e.id) if after else inverse.get(e.id, e.id),
            view="result" if after else "incoming" if e.id in inverse else "existing",
            alternative_id=e.alternative_id,
        )

    changed = {(e.kind, e.id) for c in proposal.changes for e in c.before}

    def mapping_rows(source, mapping, final, kind):
        return tuple(
            CanonicalIdMapping(input_id=old, canonical_id=mapping.get(handle, handle))
            for old, handle in source.items()
            if mapping.get(handle, handle) in final
            and (
                (kind, handle) not in changed
                or (kind == "node" and handle in preview.node_map)
            )
        )

    return GraphMergeResult(
        run_id=run_id,
        state=actual,
        diagnostics=(),
        node_mappings=mapping_rows(
            context.input_node_map, node_aliases, actual_nodes, "node"
        ),
        edge_mappings=mapping_rows(
            context.input_edge_map, edge_aliases, actual_edges, "edge"
        ),
        existing_node_mappings=mapping_rows(
            {n.id: n.id for n in context.existing.graph.nodes},
            node_aliases,
            actual_nodes,
            "node",
        ),
        existing_edge_mappings=mapping_rows(
            {e.id: e.id for e in context.existing.graph.edges},
            edge_aliases,
            actual_edges,
            "edge",
        ),
        rewrites=tuple(
            StructuralRewrite(
                before=tuple(reference(e, after=False) for e in c.before),
                after=tuple(reference(e, after=True) for e in c.after),
                explanation=c.explanation,
                base_revision=base_revision,
                result_revision=manager.revision,
            )
            for c in proposal.changes
        ),
    )
