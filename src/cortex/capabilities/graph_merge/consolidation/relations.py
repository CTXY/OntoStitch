"""Mechanical relation inheritance at the narrow node-writing boundary.

Only already confirmed ID aliases and exact edge identity are mechanical. A
formula or relation group that needs a semantic decision is handed to relation
coordination before the candidate can be accepted; this module never repairs it
by dropping sourced edges or inventing dependencies.
"""
from __future__ import annotations

from collections import defaultdict

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.operations import deduplicate_edges
from cortex.capabilities.graph_merge.graph.operations import remap_edge, remap_node
from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.schema.semantic_validation import validate_semantic_properties


class NodeRelationCoordinationRequired(ValueError):
    """A complete node draft needs coupled relation work before compilation."""

    def __init__(self, state, *, owner_ids, diagnostics, edge_aliases=None, node_aliases=None,
                 followups=()):
        self.integrated_state = state
        self.owner_ids = tuple(sorted(set(owner_ids)))
        self.diagnostics = tuple(diagnostics)
        self.edge_aliases = dict(edge_aliases or {})
        self.node_aliases = dict(node_aliases or {})
        self.integration_changes = ()  # The compiler attaches its group changes.
        self.followups = tuple(followups)
        self.details = {
            'code': 'node_relation_coordination_required',
            'owner_ids': list(self.owner_ids),
            'diagnostics': list(self.diagnostics),
            'relation_followups': list(self.followups),
            'required_action': 'Coordinate the supplied relation obligations before accepting the node candidate; do not retry the unchanged node draft.',
        }
        super().__init__('Node content requires relation coordination before structural acceptance')


def resolve_alias(ident, aliases):
    """Resolve only asserted aliases, rejecting cycles instead of guessing."""
    seen = set()
    while aliases.get(ident, ident) != ident:
        if ident in seen:
            raise ValueError('Relation alias cycle')
        seen.add(ident)
        ident = aliases[ident]
    return ident


def _properties(node):
    if node.alternatives:
        return [(a.alternative_id, GraphManager.resolve_alternative(node, a).properties)
                for a in node.alternatives]
    return [(None, node.properties)]


def _target_record(node):
    """Complete authored target meanings, without storage overrides or sources."""
    record = {'id': node.id, 'node_type': node.node_type}
    if node.alternatives:
        record['alternatives'] = [{'alternative_id': branch.alternative_id,
            'content': GraphManager.resolve_alternative(node, branch).model_dump(mode='json')}
            for branch in node.alternatives]
        record['conflict_points'] = [point.model_dump(mode='json') for point in node.conflict_points]
    else:
        record['content'] = node.model_dump(mode='json',
            include={'name', 'description', 'aliases', 'tags', 'properties'})
    return record


def _same_target_versions(before, after):
    """Exact complete versions are known; similar prose is not equivalence."""
    left, right = _target_record(before), _target_record(after)
    left.pop('id'); right.pop('id')
    # Persisted alternative order has no semantic effect, but branch IDs and
    # whole meanings must both agree to reuse existing target-version semantics.
    for record in (left, right):
        if 'alternatives' in record:
            record['alternatives'] = sorted(record['alternatives'], key=lambda row: row['alternative_id'])
    return canonical_hash(left) == canonical_hash(right)


def sync_node_relations(base, integrated, *, node_aliases, edge_aliases, owner_ids):
    """Return (state, edge aliases, mechanical Change records), or a handoff.

    The input is a freshly integrated, unpublished draft. Source routes already
    determine inheritance scopes. This function never broadens/narrows branches,
    creates a semantic edge, deletes a nonduplicate assertion, or treats two
    distinct formula operands as equivalent.
    """
    node_map = {key: resolve_alias(key, node_aliases) for key in node_aliases}
    initial_edge_map = {key: resolve_alias(key, edge_aliases) for key in edge_aliases}
    owners = {node_map.get(key, key) for key in owner_ids}
    remapped_nodes = [remap_node(node, node_map, initial_edge_map) for node in integrated.graph.nodes]
    if ({node.id for node in remapped_nodes} != {node.id for node in integrated.graph.nodes}
            or len({node.id for node in remapped_nodes}) != len(remapped_nodes)):
        raise ValueError('Node integration must finish before relation synchronization')
    remapped_edges = [remap_edge(edge, node_map) for edge in integrated.graph.edges]
    edges, dedup = deduplicate_edges(remapped_edges)
    edge_map = {key: dedup.get(value, value) for key, value in initial_edge_map.items()}
    edge_map.update(dedup)
    nodes = [remap_node(node, {}, edge_map) for node in remapped_nodes]
    state = integrated.model_copy(update={'graph': integrated.graph.model_copy(update={
        'nodes': tuple(nodes), 'edges': tuple(edges.values()),
    })})
    indexed = {node.id: node for node in state.graph.nodes}
    diagnostics = []
    problem_owners = set()
    relevant_edge_ids = set()
    # Existing union inputs have already passed fragment validation. Checking all
    # Metrics also catches consumers remapped by this known identity merge.
    for node in nodes:
        if node.node_type != 'metric':
            continue
        for alternative_id, props in _properties(node):
            # Malformed node content remains a node error, not relation work.
            validate_semantic_properties('metric', props)
            expected = set()
            for ref in props.get('expression', {}).get('refs', {}).values():
                target = indexed.get(ref)
                if target is None:
                    raise ValueError(f'Metric {node.id!r} references unknown operand {ref!r}; repair node content or supply its exact premise before coordinating relations')
                predicate = 'depends_on' if target and target.node_type in {'concept', 'metric'} else 'grounded_on'
                expected.add((predicate, ref))
            active = [edge for edge in edges.values() if edge.from_node_id == node.id
                      and edge.edge_type in {'depends_on', 'grounded_on'}
                      and (not edge.properties.get('alternative_ids')
                           or alternative_id in edge.properties['alternative_ids'])]
            actual = {(edge.edge_type, edge.to_node_id) for edge in active}
            missing, stale = expected - actual, actual - expected
            if missing or stale:
                problem_owners.add(node.id)
                relevant_edge_ids.update(edge.id for edge in active)
                diagnostics.append({'code': 'metric_operand_adjacency', 'owner_id': node.id,
                    'alternative_id': alternative_id,
                    'missing': [{'edge_type': kind, 'to_node_id': ref} for kind, ref in sorted(missing)],
                    'stale': [{'edge_type': kind, 'to_node_id': ref} for kind, ref in sorted(stale)],
                    'required_outcome': 'Preserve the complete formula meaning and reconcile its exact direct operands; retain other sourced business facts under their correct relation meaning.'})
    # Incoming edges are deliberately absent from the node Writer's default
    # packet. The controller, not that Writer, must notice when their old target
    # becomes a broader/different conflict. Source alternative_ids cannot encode
    # which target version the original assertion meant.
    before_nodes = {node.id: node for node in base.graph.nodes}
    for original_edge in base.graph.edges:
        old_target = before_nodes[original_edge.to_node_id]
        target_id = node_map.get(old_target.id, old_target.id)
        new_target = indexed.get(target_id)
        if new_target is None or not new_target.alternatives:
            continue
        live_edge = edges.get(edge_map.get(original_edge.id, original_edge.id))
        if live_edge is None or live_edge.to_node_id != target_id:
            continue
        # Compare after only confirmed reference aliases; never guess branch
        # equivalence from names, shared physical targets, evidence or role.
        mapped_old_target = remap_node(old_target, node_map, edge_map)
        if _same_target_versions(mapped_old_target, new_target):
            continue
        relevant_edge_ids.add(live_edge.id)
        problem_owners.update((target_id, live_edge.from_node_id))
        diagnostics.append({'code': 'incoming_target_conflict_scope',
            'original_edge_id': original_edge.id,
            'before_target_id': old_target.id, 'target_owner_id': target_id,
            'before_target': _target_record(old_target),
            'candidate_target': _target_record(new_target),
            'required_outcome': 'Preserve the original incoming assertion and its target-version meaning. Determine the target-version relationship from the complete original and candidate contents; do not guess or use source alternative_ids to select a target branch.'})
    if diagnostics:
        original_ids = []
        for edge in base.graph.edges:
            mapped = edge_map.get(edge.id, edge.id)
            if (mapped in relevant_edge_ids
                    or node_map.get(edge.from_node_id, edge.from_node_id) in problem_owners
                    or node_map.get(edge.to_node_id, edge.to_node_id) in problem_owners):
                original_ids.append(edge.id)
        followups = ()
        if original_ids:
            followups = ({'edge_ids': sorted(set(original_ids)),
                'problem': 'Node integration exposes relation structure requiring semantic coordination.',
                'required_outcome': 'Resolve the supplied formula, metadata and target-version diagnostics while preserving source knowledge and branch scope.'},)
        raise NodeRelationCoordinationRequired(state, owner_ids=owners | problem_owners,
            diagnostics=diagnostics, edge_aliases=edge_map, node_aliases=node_map,
            followups=followups)
    changes = []
    old_nodes = {node.id: node for node in integrated.graph.nodes}
    for node in nodes:
        if node != old_nodes[node.id]:
            changes.append(Change(before=(Element(kind='node', id=node.id),),
                after=(Element(kind='node', id=node.id),), explanation='Apply confirmed reference and edge ID aliases only.'))
    edge_origins = defaultdict(list)
    for edge in integrated.graph.edges:
        edge_origins[edge_map.get(edge.id, edge.id)].append(edge)
    for edge_id, prior in edge_origins.items():
        if len(prior) > 1 or prior[0] != edges[edge_id]:
            changes.append(Change(before=tuple(Element(kind='edge', id=edge.id) for edge in prior),
                after=(Element(kind='edge', id=edge_id),), explanation='Redirect confirmed node aliases and deduplicate exact relation assertions, retaining all sources.'))
    return state, edge_map, tuple(changes)
