"""Necessary semantic impact of actual edits, separate from signal selection."""
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL, references
from cortex.schema import GraphState

_CORE_DEPENDENCIES = frozenset({"grounded_on", "depends_on", "applies_to"})


def modification_scope(before, candidate):
    old_nodes = {n.id: n for n in before.graph.nodes}
    new_nodes = {n.id: n for n in candidate.graph.nodes}
    old_edges = {e.id: e for e in before.graph.edges}
    new_edges = {e.id: e for e in candidate.graph.edges}
    changed_nodes = {key for key in old_nodes.keys() | new_nodes.keys()
                     if old_nodes.get(key) != new_nodes.get(key)}
    changed_edges = {key for key in old_edges.keys() | new_edges.keys()
                     if old_edges.get(key) != new_edges.get(key)}
    def meaning(node):
        if node is None:
            return None
        row = node.model_dump(mode='json', exclude={'sources', 'reference_origin'})
        row['properties'].pop('relation_metadata', None)
        return row
    changed_meanings = {key for key in changed_nodes
                        if meaning(old_nodes.get(key)) != meaning(new_nodes.get(key))}
    selected_nodes = set(changed_nodes)
    selected_edges = changed_edges | _contract_affected_edges(before, candidate)
    consumers = set(semantic_review_targets(before, candidate, ())['targets'])
    for nodes, edges in ((old_nodes, old_edges), (new_nodes, new_edges)):
        # A changed endpoint does not turn every incident edge into a premise.
        # Direct consumers and complete affected metadata groups do matter.
        for node in nodes.values():
            metadata = _metadata_edges(node)
            if metadata & changed_edges:
                consumers.add(node.id)
                selected_edges.update(metadata)
        selected_nodes.update(consumers)
        for ident in consumers | changed_nodes:
            node = nodes.get(ident)
            if node is None:
                continue
            selected_nodes.update(references(node))
            if node.node_type == 'metric':
                selected_edges.update(e.id for e in edges.values()
                                      if e.from_node_id == ident and e.edge_type in {'depends_on', 'grounded_on'})
        for edge in edges.values():
            if edge.from_node_id in changed_meanings or edge.to_node_id in changed_meanings:
                selected_edges.add(edge.id)
            if edge.edge_type in {'depends_on', 'applies_to'} and edge.to_node_id in changed_nodes:
                selected_edges.add(edge.id)
            if edge.id in selected_edges:
                selected_nodes.update((edge.from_node_id, edge.to_node_id))
        # A changed binding must expose all actual competing owners of that
        # exact physical target, not the claimants' unrelated relationships.
        physical = {edges[key].to_node_id for key in changed_edges & edges.keys()
                    if edges[key].edge_type == 'grounded_on'}
        for edge in edges.values():
            if edge.edge_type == 'grounded_on' and edge.to_node_id in physical:
                selected_edges.add(edge.id)
                selected_nodes.update((edge.from_node_id, edge.to_node_id))
    return selected_nodes, selected_edges


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


def _indexes(state):
    return ({n.id: n for n in state.graph.nodes}, {e.id: e for e in state.graph.edges})

def _semantic(obj):
    # Branch sources change the justification of a competing version and remain
    # semantic. Only ordinary object-level provenance is excluded here.
    return obj.model_dump(mode="json", exclude={"sources", "reference_origin"})

def _changed(before, after):
    return {
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    }

def _semantic_changes(before, after):
    return {
        key
        for key in before.keys() | after.keys()
        if key not in before
        or key not in after
        or _semantic(before[key]) != _semantic(after[key])
    }

def _metadata_edges(node):
    props = [node.properties] + [
        {
            k.removeprefix("properties."): v
            for k, v in a.overrides.items()
            if k.startswith("properties.")
        }
        for a in node.alternatives
    ]
    return {
        edge
        for p in props
        for group in p.get("relation_metadata", ())
        for edge in group["edges"]
    }

def _contract_affected_edges(before, after):
    old = {c.id: c for c in before.graph.relation_contracts}
    new = {c.id: c for c in after.graph.relation_contracts}
    changed = _changed(old, new)
    while True:
        descendants = {
            c.id for c in (*old.values(), *new.values()) if c.extends in changed
        }
        if descendants <= changed:
            break
        changed |= descendants
    return {
        e.id
        for e in (*before.graph.edges, *after.graph.edges)
        if e.edge_type in changed
    }

def affected_query_roots(before: GraphState, after: GraphState) -> set[str]:
    """Changed definitions and both endpoints of added/removed semantic edges.

    Sources-only updates do not reopen neighbourhood queries. Callers separately
    handle new evidence; this function does not certify metadata as safe.
    """
    old_nodes, old_edges = _indexes(before)
    new_nodes, new_edges = _indexes(after)
    result = _semantic_changes(old_nodes, new_nodes)
    for key in _semantic_changes(old_edges, new_edges) | _contract_affected_edges(
        before, after
    ):
        for edges in (old_edges, new_edges):
            edge = edges.get(key)
            if edge is not None:
                result.update((edge.from_node_id, edge.to_node_id))
    return result

def _core_dependency(predicate, state):
    contracts = {c.id: c for c in state.graph.relation_contracts}
    seen = set()
    while predicate and predicate not in seen:
        if predicate in _CORE_DEPENDENCIES:
            return True
        seen.add(predicate)
        contract = contracts.get(predicate)
        predicate = contract.extends if contract else None
    return False

def semantic_review_targets(
    before: GraphState, after: GraphState, task_targets, *, evidence_changed=False
):
    """Return semantic targets and provenance flags, never a blanket shared-rule closure.

    ``metadata_only`` describes the object delta, not permission to skip review.
    Source additions/removals must be checked against the evidence previously
    reviewed for this decision. The caller owns that evidence comparison.
    """
    task_targets = set(task_targets)
    old_nodes, old_edges = _indexes(before)
    new_nodes, new_edges = _indexes(after)
    changed_nodes = _semantic_changes(old_nodes, new_nodes)
    changed_edges = _semantic_changes(old_edges, new_edges)
    contract_edges = _contract_affected_edges(before, after)
    affected = set(changed_nodes)
    for key in changed_edges | contract_edges:
        for edges, state in ((old_edges, before), (new_edges, after)):
            edge = edges.get(key)
            if edge is not None and _core_dependency(edge.edge_type, state):
                affected.add(edge.from_node_id)
    # Direct declared consumers include expression refs, relation groups and all
    # applies_to intents. Do not recursively flood the graph through a hub.
    dependency_changes = changed_nodes | changed_edges
    for nodes in (old_nodes, new_nodes):
        affected.update(
            row["consumer"]
            for row in content_consumers(nodes.values(), dependency_changes)
        )
    for edges in (old_edges, new_edges):
        affected.update(
            e.from_node_id
            for e in edges.values()
            if e.edge_type in _CORE_DEPENDENCIES and e.to_node_id in changed_nodes
        )
    if evidence_changed:
        affected.update(task_targets)
        for edges in (old_edges, new_edges):
            for edge in edges.values():
                if edge.id in task_targets:
                    affected.update((edge.from_node_id, edge.to_node_id))
    targets = {
        key
        for key in affected
        if key in new_nodes and new_nodes[key].node_type not in PHYSICAL
    }
    provenance_changed = False
    added_sources = set()
    removed_sources = set()
    for old, new in ((old_nodes, new_nodes), (old_edges, new_edges)):
        for key in old.keys() & new.keys():
            a, b = old[key], new[key]
            added_sources.update(set(b.sources) - set(a.sources))
            removed_sources.update(set(a.sources) - set(b.sources))
            provenance_changed |= a.sources != b.sources or getattr(
                a, "reference_origin", None
            ) != getattr(b, "reference_origin", None)
    contracts_changed = {c.id: c for c in before.graph.relation_contracts} != {
        c.id: c for c in after.graph.relation_contracts
    }
    semantic_changed = changed_nodes | changed_edges | contract_edges
    return {
        "targets": targets,
        "semantic_changed_ids": semantic_changed,
        "metadata_only": not semantic_changed
        and not contracts_changed
        and not evidence_changed,
        "contracts_changed": contracts_changed,
        "provenance_changed": provenance_changed,
        "added_sources": tuple(
            sorted(added_sources, key=lambda s: s.model_dump_json())
        ),
        "removed_sources": tuple(
            sorted(removed_sources, key=lambda s: s.model_dump_json())
        ),
        "requires_evidence_check": bool(added_sources or removed_sources),
        "evidence_changed": evidence_changed,
    }
