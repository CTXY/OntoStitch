"""Mechanical context selection. No predicate equivalence or business inference."""

from collections import defaultdict

from cortex.capabilities.graph_merge.inputs import PHYSICAL, references
from cortex.capabilities.graph_merge.llm.packing import bounded_items
from cortex.schema.relation_contracts import effective_schema


def content_consumers(nodes, ids):
    """Declared content references and relation groups; adjacency is a separate read."""
    targets = set(ids)
    rows = []
    for node in nodes:
        rows.extend(
            {"target": key, "consumer": node.id}
            for key in sorted(targets & references(node))
        )
        properties = [node.properties] + [
            {
                k.removeprefix("properties."): v
                for k, v in a.overrides.items()
                if k.startswith("properties.")
            }
            for a in node.alternatives
        ]
        for props in properties:
            for group in props.get("relation_metadata", ()):
                if targets & set(group["edges"]):
                    rows.append({"consumer": node.id, "relation_metadata": group})
    return rows


def active_contracts(context, state, *, edge_ids=None):
    """Join actual predicate uses with contracts, never group by guessed meaning."""
    nodes = {n.id: n for n in state.graph.nodes}
    uses = defaultdict(list)
    selected_types = set()
    for edge in state.graph.edges:
        if (
            nodes[edge.from_node_id].node_type in PHYSICAL
            and nodes[edge.to_node_id].node_type in PHYSICAL
        ):
            continue
        uses[edge.edge_type].append(edge.id)
        if edge_ids is None or edge.id in edge_ids:
            selected_types.add(edge.edge_type)
    contracts = effective_schema(context.schema, state.graph).edge_type_index()
    return [
        {
            "contract": contracts[key].model_dump(mode="json"),
            "used_by": sorted(uses[key]),
        }
        for key in sorted(selected_types)
    ]


def focused_revision(context, preview, previous, refs):
    """One-hop task packet; unknown/deleted refs fall back to full context safely.

    Review refs select roots, contracts select their actual uses, nodes select
    adjacent edges. Include endpoints, declared references and direct consumers,
    but do not recursively expand their neighborhoods. Tools retain global access.
    """
    if not refs:
        return None
    nodes = {n.id: n for n in preview.state.graph.nodes}
    edges = {e.id: e for e in preview.state.graph.edges}
    contracts = effective_schema(context.schema, preview.state.graph).edge_type_index()
    root_nodes, root_edges, predicates = set(), set(), set()
    for ref in refs:
        kind, key = ref["kind"], ref["id"]
        if kind == "contract":
            if key not in contracts:
                return None
            predicates.add(key)
        elif kind == "node":
            key = preview.node_map.get(key, key)
            if key not in nodes:
                return None
            root_nodes.add(key)
        elif kind == "edge":
            key = preview.edge_map.get(key, key)
            if key not in edges:
                return None
            root_edges.add(key)
        else:
            return None
    selected_edges = root_edges | {
        e.id
        for e in edges.values()
        if e.edge_type in predicates
        or e.from_node_id in root_nodes
        or e.to_node_id in root_nodes
    }
    selected_nodes = root_nodes | {
        key
        for eid in selected_edges
        for key in (edges[eid].from_node_id, edges[eid].to_node_id)
    }
    consumers = content_consumers(nodes.values(), root_nodes | selected_edges)
    selected_nodes.update(row["consumer"] for row in consumers)
    # Only direct reference targets, followed by catalog ownership, not semantic closure.
    selected_nodes |= {
        key
        for nid in tuple(selected_nodes)
        for key in references(nodes[nid])
        if key in nodes
    }
    physical = {key for key in selected_nodes if nodes[key].node_type in PHYSICAL}
    for key in tuple(physical):
        physical.update(
            ref
            for ref in references(nodes[key])
            if ref in nodes and nodes[ref].node_type in PHYSICAL
        )
    selected_nodes |= physical
    source_ids = {s.source_id for key in selected_nodes for s in nodes[key].sources}
    source_ids.update(s.source_id for key in selected_edges for s in edges[key].sources)
    evidence = [
        row
        for row in context.evidence_rows()
        if any(s["source_id"] in source_ids for s in row["source_refs"])
    ]
    changes = [
        c.model_dump(mode="json")
        for c in previous.changes
        if any(
            (e.kind == "node" and preview.node_map.get(e.id, e.id) in selected_nodes)
            or (e.kind == "edge" and preview.edge_map.get(e.id, e.id) in selected_edges)
            for e in (*c.before, *c.after)
        )
    ]
    alias_map = {
        k: v for k, v in preview.node_map.items() if k != v and v in selected_nodes
    }
    used = active_contracts(context, preview.state, edge_ids=selected_edges)
    shown = {row["contract"]["id"] for row in used}
    return {
        "revision_scope": {
            "refs": refs,
            "node_count": len(selected_nodes - physical),
            "edge_count": len(selected_edges),
            "global_reads_available": True,
        },
        "candidate_nodes": [
            nodes[k].model_dump(mode="json") for k in sorted(selected_nodes - physical)
        ],
        "candidate_relations": [
            edges[k].model_dump(mode="json") for k in sorted(selected_edges)
        ],
        "physical_facts": bounded_items(
            [
                nodes[k].model_dump(mode="json", exclude_defaults=True)
                for k in sorted(physical)
            ],
            budget=8000,
        ),
        "active_contracts": used,
        "local_contracts": [
            contracts[k].model_dump(mode="json") for k in sorted(predicates - shown)
        ],
        "consumers": consumers,
        "evidence": bounded_items(evidence),
        "evidence_directory": [
            {
                "id": row["id"],
                "source_ids": sorted({s["source_id"] for s in row["source_refs"]}),
            }
            for row in evidence
        ],
        "prior_changes": changes,
        "identity_map": alias_map,
    }
