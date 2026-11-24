"""Compose candidate-relative edits into the existing source-relative compiler contract.

No semantic rewrite is inferred. Correspondence still names immutable source
handles, and the full cumulative proposal passes the original compiler.
"""

from cortex.capabilities.graph_merge.graph.publication import mapped_base
from cortex.capabilities.graph_merge.inputs import remap_edge, remap_node
from cortex.capabilities.graph_merge.graph.edit_contract import (
    GraphEdit,
    IntegrationProposal,
)
from cortex.schema import GraphEdge, GraphNode
from cortex.schema.relation_contracts import merge_relation_contracts


def graph_delta(before, after):
    result = {}
    for label in ("nodes", "edges"):
        old = {x.id: x for x in getattr(before.graph, label)}
        new = {x.id: x for x in getattr(after.graph, label)}
        result["relations" if label == "edges" else label] = {
            "created": sorted(new.keys() - old.keys()),
            "updated": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
            "deleted": sorted(old.keys() - new.keys()),
        }
    old = {x.id: x for x in before.graph.relation_contracts}
    result["changed_contracts"] = [
        x.id for x in after.graph.relation_contracts if old.get(x.id) != x
    ]
    return result


def graph_edits(before_nodes, before_edges, nodes, edges):
    """Serialize the exact net graph difference; do not infer correspondence."""
    edits = []
    for kind, old, new in (
        ("node", before_nodes, nodes),
        ("edge", before_edges, edges),
    ):
        for key in sorted(old.keys() - new.keys()):
            edits.append(
                GraphEdit(**{"operation": "delete_" + kind, kind + "_id": key})
            )
        for key in sorted(new):
            if old.get(key) != new[key]:
                edits.append(
                    GraphEdit(
                        **{
                            "operation": ("update_" if key in old else "create_")
                            + kind,
                            kind: new[key].model_dump(mode="json"),
                        }
                    )
                )
    return tuple(edits)


def compose_revision(context, decision, previous, preview, patch):
    """Validate patch operations against the visible candidate, then emit a net patch."""
    base_nodes, base_edges, _, _ = mapped_base(context, decision)
    nodes = {x.id: x for x in preview.state.graph.nodes}
    edges = {x.id: x for x in preview.state.graph.edges}
    edited = set()
    for edit in patch.edits:
        kind = (
            "node"
            if edit.operation.endswith("node") or edit.operation == "set_conflict"
            else "edge"
        )
        objects, model, aliases = (
            (nodes, GraphNode, preview.node_map)
            if kind == "node"
            else (edges, GraphEdge, preview.edge_map)
        )
        raw = getattr(edit, kind)
        raw_id = getattr(edit, kind + "_id") if raw is None else raw["id"]
        key = aliases.get(raw_id, raw_id)
        if (kind, key) in edited:
            raise ValueError(f"One operation per candidate element: {key}")
        edited.add((kind, key))
        old = objects.get(key)
        if edit.operation.startswith("create_"):
            if old is not None or key in (base_nodes if kind == "node" else base_edges):
                raise ValueError(f"Create requires a fresh ID: {key}")
        elif old is None:
            raise ValueError(f"Cannot {edit.operation}: missing candidate {key}")
        if edit.operation.startswith("delete_"):
            del objects[key]
            continue
        data = {**raw, "id": key}
        if old is not None and "sources" not in data:
            data["sources"] = [s.model_dump(mode="json") for s in old.sources]
        obj = model.model_validate(data)
        if kind == "node":
            if old is not None and obj.node_type != old.node_type:
                raise ValueError(f"Existing node type cannot change for {key}")
            obj = remap_node(obj, preview.node_map, preview.edge_map)
        else:
            obj = remap_edge(obj, preview.node_map)
        objects[key] = obj

    # Replacement correspondence must cover the whole previous before-group.
    # In particular, never silently drop one source of a many-to-many rewrite.
    replaced = {e for change in patch.changes for e in change.before}
    changes = []
    for change in previous.changes:
        overlap = set(change.before) & replaced
        if overlap and not set(change.before) <= replaced:
            raise ValueError(
                "Replace all before elements of an affected correspondence group"
            )
        if not overlap:
            changes.append(change)
    changes.extend(patch.changes)

    return IntegrationProposal(
        edits=graph_edits(base_nodes, base_edges, nodes, edges),
        relation_contracts=merge_relation_contracts(
            previous.relation_contracts, patch.relation_contracts
        ),
        changes=tuple(changes),
        identity_feedback=patch.identity_feedback,
        representation_error=patch.representation_error,
    )
