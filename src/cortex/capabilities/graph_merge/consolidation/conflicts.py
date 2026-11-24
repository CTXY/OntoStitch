"""Execute an Agent-declared existing-branch selection; never choose a branch."""
from pydantic import Field
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.models import Model
from cortex.schema.graph import alternative_sources


class ConflictResolution(Model):
    node_id: str
    keep_alternative_id: str
    discard_reasons: dict[str, str] = Field(description="Every other existing alternative ID and its specific evidenced correction/redundancy reason. Supported competing choices must remain a conflict.")


def resolution_edits(current, resolutions):
    nodes = {n.id: n for n in current.graph.nodes}
    edits, changes = [], []
    for resolution in resolutions:
        owner = nodes[resolution.node_id]
        alternatives = {a.alternative_id: a for a in owner.alternatives}
        keep = resolution.keep_alternative_id
        if keep not in alternatives or set(resolution.discard_reasons) != alternatives.keys() - {keep}:
            raise ValueError(f"Resolution for {owner.id} must keep one existing alternative and explain every other ID: {sorted(alternatives)}")
        if any(not v.strip() for v in resolution.discard_reasons.values()):
            raise ValueError("Each discarded alternative requires an evidenced reason")
        content = GraphManager.resolve_alternative(owner, alternatives[keep])
        node = owner.without_conflict().model_copy(update={**content.model_dump(mode="json"),
            "sources": alternative_sources(owner, alternatives[keep])})
        edits.append({"operation": "update_node", "node": node.model_dump(mode="json")})
        for alternative_id in alternatives:
            changes.append({"before": [{"kind": "node", "id": owner.id, "alternative_id": alternative_id}],
                "after": [{"kind": "node", "id": owner.id}] if alternative_id == keep else [],
                "explanation": "Retain selected existing content and exact attribution." if alternative_id == keep else resolution.discard_reasons[alternative_id]})
        for edge in current.graph.edges:
            scope = edge.properties.get("alternative_ids", [])
            if edge.from_node_id != owner.id or not scope:
                continue
            if keep in scope:
                props = {**edge.properties, "alternative_ids": None}
                edits.append({"operation": "update_edge", "edge": edge.model_copy(update={"properties": props}).model_dump(mode="json")})
            else:
                edits.append({"operation": "delete_edge", "edge_id": edge.id})
                changes.append({"before": [{"kind": "edge", "id": edge.id}], "after": [],
                    "explanation": "; ".join(resolution.discard_reasons[k] for k in scope)})
    return edits, changes
