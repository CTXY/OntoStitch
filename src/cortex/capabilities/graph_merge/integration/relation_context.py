"""Compact relationship views over a complete controller-owned graph.

No inferred business edges, field-name classifiers or source broadcasting.
"""
from collections import defaultdict

from cortex.capabilities.graph_merge.integration.batch import delta
from cortex.capabilities.graph_merge.integration.context import PHYSICAL, references
from cortex.schema.relation_contracts import effective_schema


def indexes(state):
    return ({n.id: n for n in state.graph.nodes}, {e.id: e for e in state.graph.edges})


def node_view(node, *, full=False, evidence_ids=None):
    result = {"id": node.id, "node_type": node.node_type, "name": node.name,
              "aliases": node.aliases}
    keys = ("definition", "action", "condition", "relation_metadata")
    properties = dict(node.properties) if full else {k: node.properties[k] for k in keys if k in node.properties}
    if properties:
        result["properties"] = properties
    if node.description and node.description != node.properties.get("definition"):
        result["description"] = node.description
    if node.alternatives:
        result["alternative_ids"] = [a.alternative_id for a in node.alternatives]
        result["conflict_points"] = [p.model_dump(mode="json") for p in node.conflict_points]
    common = getattr(node, "common_sources", ())
    if common and evidence_ids is not None:
        result["common_evidence_ids"] = sorted({i for source in common for i in evidence_ids.get(source, ())})
    return result


def physical_view(node, nodes, edges=()):
    """A physical ID is readable without exposing samples or catalog metadata."""
    row = {"id": node.id, "node_type": node.node_type, "name": node.name}
    if node.node_type == "table":
        row["table"] = node.properties.get("table_name", node.name)
    elif node.node_type == "column":
        table = nodes.get(node.properties.get("table_ref"))
        row["table"] = table.properties.get("table_name", table.name) if table else node.properties.get("table_ref")
        row["column"] = node.properties.get("column_name", node.name)
    elif node.node_type == "dimension_value":
        column = nodes.get(node.properties.get("column_ref"))
        if column is not None:
            parent = physical_view(column, nodes)
            row.update({key: parent[key] for key in ("table", "column") if key in parent})
        row["value"] = node.properties.get("value", node.name)
    owners = [edge for edge in edges if edge.edge_type == "grounded_on" and edge.to_node_id == node.id
              and nodes[edge.from_node_id].node_type == "concept"]
    if owners:
        row["owners"] = [{"id": edge.from_node_id, "edge_id": edge.id,
                          **({"alternative_ids": edge.properties["alternative_ids"]}
                             if edge.properties.get("alternative_ids") else {})} for edge in owners]
    return row


def edge_view(edge, evidence_ids):
    row = {"id": edge.id, "from_node_id": edge.from_node_id, "edge_type": edge.edge_type,
           "to_node_id": edge.to_node_id, "properties": dict(edge.properties),
           "evidence_ids": sorted({i for s in edge.sources for i in evidence_ids.get(s, ())})}
    return row


def relation_context(context, current, task, *, before=None, assessment=False):
    context.candidate = current
    if before is not None:
        context.task_before = before
    context.phase = task.phase
    nodes, edges = indexes(current)
    old_nodes, old_edges = indexes(before) if before is not None else ({}, {})
    changes = delta(before, current) if before is not None else None
    roots = set(task.targets) & nodes.keys()
    chosen = set(task.targets) & edges.keys()
    if assessment:
        changed_nodes = set(changes["nodes"]["created"] + changes["nodes"]["updated"])
        changed_edges = set(changes["edges"]["created"] + changes["edges"]["updated"])
        old_changed_edges = set(changes["edges"]["deleted"] + changes["edges"]["updated"])
        roots = changed_nodes | {
            k for eid in changed_edges for k in (edges[eid].from_node_id, edges[eid].to_node_id)
        } | {
            k for eid in old_changed_edges
            for k in (old_edges[eid].from_node_id, old_edges[eid].to_node_id)
        }
        from cortex.capabilities.graph_merge.integration.task_context import semantic_review_targets
        roots.update(semantic_review_targets(before, current, task.targets)['targets'])
        chosen = changed_edges | {
            e.id for e in edges.values()
            if (e.from_node_id in roots and e.to_node_id in roots)
            or e.from_node_id in changed_nodes or e.to_node_id in changed_nodes
        }
        # A grounding repair must show the remaining canonical owner as well
        # as the deleted assertion; otherwise the reviewer sees false loss.
        physical_roots = {k for k in roots if k in nodes and nodes[k].node_type in PHYSICAL}
        chosen |= {e.id for e in edges.values()
                   if e.edge_type == "grounded_on" and e.to_node_id in physical_roots}
        # Deleting an incorrect physical input must show the surviving direct
        # semantic operands too. An unchanged Metric node is not changed_nodes,
        # so the generic change projection otherwise hides these retained edges.
        affected_metrics = {k for k in roots if k in nodes and nodes[k].node_type == "metric"}
        chosen |= {e.id for e in edges.values()
                   if e.from_node_id in affected_metrics
                   and e.edge_type in {"depends_on", "grounded_on"}}
    else:
        chosen |= {e.id for e in edges.values()
                   if (e.from_node_id in roots or e.to_node_id in roots)
                   and nodes[e.from_node_id].node_type not in PHYSICAL
                   and nodes[e.to_node_id].node_type not in PHYSICAL
                   and (e.edge_type not in {"applies_to", "depends_on"}
                        or e.from_node_id in roots)}
    # Group members are indivisible; metadata refers to authored edge IDs.
    while True:
        prior = set(chosen)
        for n in nodes.values():
            groups = n.properties.get("relation_metadata", [])
            for group in groups:
                members = set(group["edges"])
                if n.id in roots or members & chosen:
                    chosen |= members & edges.keys()
                    roots.add(n.id)
        if prior == chosen:
            break
    selected = roots | {k for eid in chosen for k in
                        (edges[eid].from_node_id, edges[eid].to_node_id)}
    # Matching/formula references are necessary premises, even when a direct
    # grounding edge is intentionally owned by another Concept.
    physical = {ref for key in selected if key in nodes
                for ref in references(nodes[key]) if ref in nodes and nodes[ref].node_type in PHYSICAL}
    physical |= {key for key in selected if key in nodes and nodes[key].node_type in PHYSICAL}
    chosen |= {edge.id for edge in edges.values()
               if edge.edge_type == "grounded_on" and edge.to_node_id in physical}
    selected |= physical | {key for eid in chosen for key in (edges[eid].from_node_id, edges[eid].to_node_id)}
    refs = {s for k in selected if k in nodes for s in nodes[k].sources}
    refs |= {s for k in selected if k in nodes for a in nodes[k].alternatives for s in a.sources}
    refs |= {s for eid in chosen for s in edges[eid].sources}
    if assessment:
        refs |= {s for k in changes["nodes"]["deleted"] for s in old_nodes[k].sources}
        refs |= {s for k in changes["edges"]["deleted"] for s in old_edges[k].sources}
    evidence = [e for e in context.evidence if e.source in refs]
    evidence_ids = defaultdict(list)
    rows = {}
    for e in evidence:
        evidence_ids[e.source].append(e.id)
        rows.setdefault(e.id, {"id": e.id, "content": e.content})
    contracts = effective_schema(context.schema, current.graph).edge_type_index()
    predicates = {edges[k].edge_type for k in chosen}
    packet = {
        "view": "light_relations_v1", "phase": task.phase, "task_id": task.id,
        "revision": getattr(context, "workspace_revision", 0),
        "presentation": "Focus properties are complete; neighbours are summaries. Unshown fields remain readable by exact path.",
        "nodes": [
            (physical_view(nodes[k], nodes, edges.values())
             if nodes[k].node_type in PHYSICAL else node_view(nodes[k], full=k in roots, evidence_ids=evidence_ids))
            for k in sorted(selected) if k in nodes
        ],
        "edges": [edge_view(edges[k], evidence_ids) for k in sorted(chosen)],
        "contracts": [{"id": k, "description": contracts[k].description,
                       "from_node_types": contracts[k].from_node_types,
                       "to_node_types": contracts[k].to_node_types}
                      for k in sorted(predicates) if k in contracts],
        "evidence": list(rows.values()),
        "feedback": task.feedback,
        "allowed_inference": ["is_a transitivity, same scope and compatible branches only"],
    }
    # Summarize resolved branch meaning once; full business contents remain
    # available through selective reads, never inferred from common placeholders.
    from cortex.application.ontology_graph import GraphManager
    packet["current_conflict_views"] = [
        {"node_id": key,
         **({"common_evidence_ids": sorted({i for source in getattr(nodes[key], "common_sources", ()) for i in evidence_ids.get(source, ())})}
            if getattr(nodes[key], "common_sources", ()) else {}),
         "alternatives": [
            {"alternative_id": alternative.alternative_id,
             "resolved_content": (GraphManager.resolve_alternative(nodes[key], alternative).model_dump(mode="json")
                                  if key in roots else semantic_summary(GraphManager.resolve_alternative(nodes[key], alternative))),
             "evidence_ids": sorted({i for ref in alternative.sources for i in evidence_ids.get(ref, ())}),
             "active_outgoing_edge_ids": sorted(
                 e.id for e in edges.values() if e.from_node_id == key
                 and (not e.properties.get("alternative_ids")
                      or alternative.alternative_id in e.properties["alternative_ids"]))}
            for alternative in nodes[key].alternatives]}
        for key in sorted(selected & nodes.keys()) if nodes[key].alternatives
    ]
    if packet["current_conflict_views"]:
        packet["conflict_record_semantics"] = (
            "The owner contains common fields plus whole-field alternative overrides. "
            "Its common action/reason may be null and its name may be the owner ID when these vary. "
            "resolved_content is a lightweight semantic summary, not a complete-field record; omitted fields are available on demand. Branch behavior is evaluated separately, "
            "one alternative at a time; common nulls/placeholders do not prove a missing field."
        )
    if assessment:
        packet["actual_changes"] = changes
        packet["before_nodes"] = [node_view(old_nodes[k], full=True, evidence_ids=evidence_ids) for k in
                                  changes["nodes"]["updated"] + changes["nodes"]["deleted"]]
        packet["before_edges"] = [edge_view(old_edges[k], evidence_ids) for k in
                                  changes["edges"]["updated"] + changes["edges"]["deleted"]]
        packet["new_contracts"] = [
            c.model_dump(mode="json") for c in current.graph.relation_contracts
            if c not in before.graph.relation_contracts
        ]
    else:
        packet["scope"] = {"targets": sorted(set(task.targets) & (nodes.keys() | edges.keys())),
                           "writable_ids": sorted((set(task.targets) & nodes.keys()) | chosen)}
        # Batches were scheduled before previous local writes. Signals recall
        # the current region; retired IDs are not current objects or obligations.
        def current_refs(value):
            if isinstance(value, str):
                return {value} if value in nodes or value in edges else set()
            if isinstance(value, dict):
                return set().union(*(current_refs(v) for v in value.values())) if value else set()
            if isinstance(value, (list, tuple)):
                return set().union(*(current_refs(v) for v in value)) if value else set()
            return set()

        packet["signals"] = [
            {"kind": c["kind"], "node_ids": sorted(current_refs(c) & nodes.keys()),
             "edge_ids": sorted(current_refs(c) & edges.keys())}
            for c in task.checks if current_refs(c)
        ]
        # Physical owners are a small semantic hint, not full binding/catalog metadata.
        domains = defaultdict(set)
        direct = defaultdict(set)
        for edge in edges.values():
            if edge.edge_type == "grounded_on":
                direct[edge.to_node_id].add(edge.from_node_id)
                p = nodes[edge.to_node_id]
                if p.node_type == "dimension_value" and edge.from_node_id in selected:
                    domains[p.properties.get("column_ref")].add(edge.from_node_id)
        packet["value_domains"] = [
            {"column_id": k, "column": nodes[k].properties.get("column_name", nodes[k].name),
             "value_concepts": sorted(v), "column_concepts": sorted(direct[k])}
            for k, v in sorted(domains.items()) if k in nodes and len(v) > 1
        ]
    # Evidence may belong to edges alone. Collect before presenting, and render
    # each body once after the graph, scopes and change information.
    from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
    presenter = EvidencePresentation(context)
    selected_evidence = [e["id"] for e in packet.pop("evidence")]
    packet["evidence"] = presenter.read(selected_evidence)["items"]
    return packet


def semantic_summary(content):
    """Only relationship-relevant meaning, never the complete formula/reason twice."""
    row = {"name": content.name, "aliases": content.aliases}
    if content.description:
        row["description"] = content.description
    row["properties"] = {k: v for k, v in content.properties.items()
                         if k in {"definition", "condition", "action", "relation_metadata"}}
    return row


def relation_signature(context, current, task):
    """Exact accepted-region coverage, independent of task IDs or dialogue state."""
    import hashlib
    import json

    from cortex.capabilities.graph_merge.integration.engine import (
        implementation_version,
    )

    packet = relation_context(context, current, task)
    ids = {n["id"] for n in packet["nodes"]}
    content = {k: packet[k] for k in ("edges", "contracts", "evidence", "signals")}
    content["nodes"] = [n.model_dump(mode="json") for n in current.graph.nodes if n.id in ids]
    # An explicitly audited infrastructure-only rollout may retain the prior
    # review policy. Actual loaded-code hashes are still recorded separately.
    content["implementation"] = getattr(context, "review_policy_version", None) or implementation_version()
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
