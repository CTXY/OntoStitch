"""Two bounded, read-only tools over the current merge input snapshot."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from cortex.capabilities.graph_merge.workflow.context import GroupContext


def build_read_tools(context: GroupContext):
    def bounded(value):
        if len(json.dumps(value, ensure_ascii=False)) > context.max_context_characters:
            return {
                "status": "too_large",
                "instruction": "Request fewer IDs; this result is incomplete.",
            }
        return value

    @tool
    def read_evidence(ids: list[str]) -> dict:
        """Read original question/gold SQL by evidence ID or source ID from this merge only."""
        if len(ids) > context.max_evidence_items:
            return {"status": "too_large", "instruction": "Request fewer evidence IDs."}
        found = [
            e for e in context.evidence if e.id in ids or e.source.source_id in ids
        ]
        known = {key for e in found for key in (e.id, e.source.source_id)}
        return bounded(
            {
                "items": [e.model_dump(mode="json") for e in found],
                "missing_ids": sorted(set(ids) - known),
            }
        )

    @tool
    def read_nodes(
        ids: list[str], include_relations: bool = True, view: str = "existing"
    ) -> dict:
        """Read complete nodes/alternatives and outgoing relations from the fixed input graph.

        Follow returned target IDs to inspect dependencies. This never edits or rematches nodes.
        """
        if view not in {"existing", "input", "candidate"}:
            return {
                "status": "invalid_view",
                "allowed": ["existing", "input", "candidate"],
            }
        state = (
            context.candidate_state if view == "candidate" else context.existing_state
        )
        if view == "candidate" and state is None:
            return {
                "status": "not_available",
                "instruction": "Candidate preview is available to Assessment only.",
            }
        selected_nodes = (
            context.input_nodes
            if view == "input"
            else state.graph.nodes
            if state is not None
            else context.nodes
        )
        selected_edges = (
            context.input_relations
            if view == "input"
            else state.graph.edges
            if state is not None
            else context.relations
        )
        nodes = {n.id: n for n in selected_nodes}
        members = {m.ref.ref_id: m for m in context.group.members}
        items = []
        for node_id in dict.fromkeys(ids):
            if node_id in nodes:
                item = {"view": view, "node": nodes[node_id].model_dump(mode="json")}
            elif node_id in members:
                item = {"representation": members[node_id].model_dump(mode="json")}
            else:
                items.append({"id": node_id, "status": "not_found"})
                continue
            variants = context.dependency_variants.get(node_id, ())
            if variants:
                item["related_input_definitions"] = [
                    nodes[ref].model_dump(mode="json")
                    for ref in variants
                    if ref in nodes and ref != node_id
                ]
            if include_relations:
                item["outgoing_relations"] = [
                    e.model_dump(mode="json")
                    for e in selected_edges
                    if e.from_node_id == node_id
                ]
                item["incoming_relations"] = [
                    e.model_dump(mode="json")
                    for e in selected_edges
                    if e.to_node_id == node_id
                ]
            items.append(item)
        return bounded({"items": items})

    return [read_evidence, read_nodes]
