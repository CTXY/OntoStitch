"""Controller-owned scope checks over explicit patch objects, not LLM permissions."""
import json

from cortex.capabilities.graph_merge.graph.operations import PHYSICAL, references
from cortex.capabilities.graph_merge.reconciliation.context import node_view, edge_view, physical_view
from cortex.application.ontology_graph import GraphManager


class ScopeContextRequired(ValueError):
    def __init__(self, context):
        super().__init__("This coupled change needs the supplied current context. Nothing was applied; use it to return the corrected local Patch.")
        self.context = context


def edited_ids(result):
    ids = set()
    for edit in result.edits:
        data = edit.model_dump()
        body = data.get("node", data.get("edge", {}))
        ids.add(body.get("id", data.get("node_id", data.get("edge_id"))))
    ids.update(c.node_id for c in result.conflicts)
    ids.update(c.node_id for c in result.resolutions)
    for merge in result.edge_merges:
        ids.update((merge.survivor_id, *merge.source_ids))
    ids.update(ref.id for change in result.changes for ref in (*change.before, *change.after))
    return ids - {None}


class ProposalScope:
    """Admit only named, locally coupled objects; a read never grants write access."""

    def __init__(self, context, payload, evidence):
        self.context, self.evidence = context, evidence
        batch = context.active_batch
        self.anchors = set(batch.targets)
        current_keys = ("nodes", "edges") if payload.get("view") == "light_relations_v1" else ("candidate_nodes", "candidate_relations")
        self.visible = {r["id"] for key in current_keys for r in payload.get(key, [])}

    def observe(self, rows, view="candidate"):
        if view == "candidate":
            self.visible.update(r["id"] for r in rows if "id" in r)

    def prepare(self, result):
        batch = self.context.active_batch
        state = batch.preview or batch.seed
        nodes = {n.id: n for n in state.graph.nodes}
        edges = {e.id: e for e in state.graph.edges}
        requested = {batch.aliases.get(k, batch.edge_aliases.get(k, k)) for k in edited_ids(result)}
        outside = requested & (nodes.keys() | edges.keys()) - batch.allowed
        endpoints = {key for edit in result.edits if hasattr(edit, "edge")
                     for field in ("from_node_id", "to_node_id")
                     if (key := edit.edge.get(field)) in nodes}
        if not outside and endpoints <= self.visible:
            return set()
        roots = (self.anchors & nodes.keys()) | {k for e in edges.values() if e.id in self.anchors
                                               for k in (e.from_node_id, e.to_node_id)}
        roots = {k for k in roots if nodes[k].node_type not in PHYSICAL}
        local_nodes = set(roots)
        # An explicit local replacement couples its named successors even when
        # a duplicate is isolated and therefore has no graph path to them.
        # This grants neither unrelated writes nor permission through reads.
        for change in result.changes:
            before = {ref.id for ref in change.before}
            if before and before <= batch.allowed:
                local_nodes.update(ref.id for ref in change.after
                                   if ref.kind == "node" and ref.id in nodes
                                   and nodes[ref.id].node_type not in PHYSICAL)
        for edge in edges.values():
            ends = {edge.from_node_id, edge.to_node_id}
            if ends & roots and all(nodes[k].node_type not in PHYSICAL for k in ends):
                local_nodes.update(ends)
        for node in nodes.values():
            if node.node_type not in PHYSICAL and (
                references(node) & roots
                or any(set(g["edges"]) & batch.allowed
                       for g in node.properties.get("relation_metadata", []))
            ):
                local_nodes.add(node.id)
        local_edges = {e.id for e in edges.values()
                       if ({e.from_node_id, e.to_node_id} & (roots | (requested & local_nodes))
                           or (e.id in requested and {e.from_node_id, e.to_node_id} & local_nodes))
                       and not all(nodes[k].node_type in PHYSICAL for k in (e.from_node_id, e.to_node_id))}
        unrelated = outside - local_nodes - local_edges
        if unrelated:
            raise ValueError(f"Outside this local task: {sorted(unrelated)}. Return only the coupled change; unrelated work is not implicitly authorized by reading it.")
        missing = (outside | endpoints) - self.visible
        if missing:
            packet = {"view": "candidate", "nodes": [physical_view(nodes[k], nodes, edges.values()) if nodes[k].node_type in PHYSICAL
                                                      else node_view(nodes[k], evidence_ids=self.evidence.by_source)
                                                      for k in sorted(missing & nodes.keys())],
                      "edges": [edge_view(edges[k], self.evidence.by_source) for k in sorted(missing & edges.keys())]}
            # Include the exact old property values a partial writer proposes
            # to replace, rather than demanding a second inspection round.
            updates = {e.node["id"]: e.node for e in result.edits if hasattr(e, "node")}
            for row in packet["nodes"]:
                fields = updates.get(row["id"], {}).get("properties", {})
                current_properties = nodes[row["id"]].properties
                # RelationPatch currently compiles updates into full records.
                # Return only properties actually changed, not every inherited
                # binding/formula merely carried by that internal record.
                changed = {k for k, value in fields.items()
                           if value != current_properties.get(k)
                           or (value is None and k in current_properties)}
                if changed:
                    row.setdefault("properties", {}).update({k: current_properties.get(k) for k in changed})
            refs = [s for k in missing for s in (nodes.get(k) or edges[k]).sources]
            for row in packet["nodes"]:
                node = nodes[row["id"]]
                if node.alternatives:
                    row["alternatives"] = [{"alternative_id": a.alternative_id,
                        "content": GraphManager.resolve_alternative(node, a).model_dump(mode="json"),
                        "evidence_ids": self.evidence.ids_for(a.sources)} for a in node.alternatives]
                    refs.extend(s for a in node.alternatives for s in a.sources)
            prior_seen = set(self.evidence.seen)
            self.evidence.append(packet, self.evidence.ids_for(refs))
            if len(json.dumps(packet, ensure_ascii=False)) > 60000:
                self.evidence.seen = prior_seen
                raise ValueError("Required coupled context exceeds the local packet size; keep the batch unpublished and rebuild a dependency-complete task.")
            self.visible.update(missing)
            raise ScopeContextRequired(packet)
        return outside


def scope_error(error):
    packet = getattr(error, "context", None)
    return {"required_context": packet} if packet is not None else {}
