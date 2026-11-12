"""Run-local check coverage and compact, explicit source/current packets."""

import hashlib
import json

from cortex.capabilities.graph_merge.integration.task_context import _indexes


def signal_targets(check, state):
    nodes, edges = _indexes(state)
    keys = set(check.get("node_ids", ())) | set(check.get("edge_ids", ()))
    keys |= {x.get("concept") for x in check.get("members", ())}
    for key in check.get("edge_ids", ()):
        if key in edges:
            keys.update((edges[key].from_node_id, edges[key].to_node_id))
    if check.get("kind") == "source_selection_variants":
        operands = set(keys)
        keys.update(
            e.from_node_id
            for e in edges.values()
            if e.edge_type == "applies_to" and e.to_node_id in operands
        )
    return keys & (nodes.keys() | edges.keys())


def _stamp(state, keys, check=None):
    """Run-local coverage only; changed incident facts reopen a check."""
    nodes, edges = _indexes(state)
    selected_edges = {
        k
        for k, e in edges.items()
        if k in keys or e.from_node_id in keys or e.to_node_id in keys
    }
    data = [
        check,
        [nodes[k].model_dump(mode="json") for k in sorted(keys & nodes.keys())],
        [edges[k].model_dump(mode="json") for k in sorted(selected_edges)],
        [
            c.model_dump(mode="json")
            for c in state.graph.relation_contracts
            if any(edges[k].edge_type == c.id for k in selected_edges)
        ],
    ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _compact_packet(payload):
    """Remove display directories and exact duplicate source contents only.

    Source/current remain distinct views. Equal source objects are represented
    by explicit ID references, never silently treated as missing knowledge.
    """
    for source, current in (
        ("nodes", "candidate_nodes"),
        ("relations", "candidate_relations"),
    ):
        by_id = {x["id"]: x for x in payload[current]}
        same = [x["id"] for x in payload[source] if by_id.get(x["id"]) == x]
        payload[source] = [x for x in payload[source] if x["id"] not in same]
        payload.setdefault("unchanged_source_objects", {})[source] = same
    for key in ("neighbor_directory", "evidence_directory", "consumer_directory"):
        payload.pop(key, None)
    payload.pop("earlier_findings", None)
    return payload
