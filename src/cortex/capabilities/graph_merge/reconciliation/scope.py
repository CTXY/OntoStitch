"""Signal-specific, nonrecursive reconciliation scopes.

A query names a complete relation set, not an adjacency grant. Endpoint bodies
are readable context; they never become expansion anchors merely by appearing.
"""
import json
from dataclasses import dataclass, field

from cortex.capabilities.graph_merge.inputs import PHYSICAL, references
from cortex.schema.relation_contracts import authoring_schema, effective_schema


@dataclass
class SignalScope:
    node_ids: set[str] = field(default_factory=set)
    edge_ids: set[str] = field(default_factory=set)
    core_node_ids: set[str] = field(default_factory=set)
    writable_node_ids: set[str] = field(default_factory=set)
    writable_edge_ids: set[str] = field(default_factory=set)
    metadata_only_node_ids: set[str] = field(default_factory=set)
    contract_ids: set[str] = field(default_factory=set)
    queries: list[dict] = field(default_factory=list)


def _groups(node):
    from cortex.application.ontology_graph import GraphManager
    contents = [node, *(GraphManager.resolve_alternative(node, alt) for alt in node.alternatives)]
    return [set(group.get('edges', ())) for content in contents
            for group in content.properties.get('relation_metadata', ())]


def signal_scope(context, current, checks):
    """Select exact signal premises and grants; union does not expand endpoints."""
    if isinstance(checks, dict):
        checks = [checks]
    from cortex.capabilities.graph_merge.reconciliation.policy import (
        OBLIGATIONS,
        SIGNALS,
    )
    nodes = {node.id: node for node in current.graph.nodes}
    edges = {edge.id: edge for edge in current.graph.edges}
    scope = SignalScope()

    def endpoints(ids):
        return {ident for key in ids if key in edges for ident in
                (edges[key].from_node_id, edges[key].to_node_id)}

    def node_query(name, ids, description, **selector):
        ids = set(ids)
        scope.node_ids.update(ids)
        scope.queries.append({'kind': name, **selector, 'node_ids': sorted(ids), 'description': description})
        return ids

    def edge_query(name, predicate, description, **selector):
        ids = {key for key, edge in edges.items() if predicate(edge)}
        scope.edge_ids.update(ids)
        scope.queries.append({'kind': name, **selector, 'edge_ids': sorted(ids), 'description': description})
        return ids

    def explicit_refs(record):
        found_nodes, found_edges = set(), set()
        def walk(row):
            if isinstance(row, dict):
                for name, value in row.items():
                    if name in {'node_id', 'owner_id', 'target_owner_id', 'physical_id', 'column_id', 'concept', 'value'} and isinstance(value, str):
                        found_nodes.add(value)
                    elif name == 'edge_id' and isinstance(value, str):
                        found_edges.add(value)
                    elif name in {'node_ids', 'owner_ids', 'member_ids', 'anchor_ids', 'consumer_ids', 'shared_operands'} and isinstance(value, (list, tuple)):
                        found_nodes.update(x for x in value if isinstance(x, str))
                    elif name == 'edge_ids' and isinstance(value, (list, tuple)):
                        found_edges.update(x for x in value if isinstance(x, str))
                    elif name == 'refs' and isinstance(value, list):
                        for ref in value:
                            if isinstance(ref, dict) and isinstance(ref.get('id'), str):
                                if ref.get('kind') == 'contract':
                                    scope.contract_ids.add(ref['id'])
                                else:
                                    (found_edges if ref.get('kind') == 'edge' else found_nodes).add(ref['id'])
                    if isinstance(value, (dict, list, tuple)):
                        walk(value)
            elif isinstance(row, (list, tuple)):
                for child in row:
                    walk(child)
        walk(record)
        return found_nodes, found_edges

    for check in checks:
        kind = check.get('kind')
        if kind not in {*SIGNALS, *OBLIGATIONS, 'concept_metadata_repair', 'node_relation_obligations'}:
            raise ValueError('Unsupported reconciliation signal/obligation kind: ' + str(kind))
        anchors, nominated = explicit_refs(check)
        scope.edge_ids.update(nominated)
        anchors |= endpoints(nominated)
        core = set(anchors)
        if kind in {'triangle', 'parallel_assertions'}:
            pairs = {(edges[e].from_node_id, edges[e].to_node_id) for e in nominated if e in edges}
            if not pairs:
                pairs = {(a, b) for a in anchors for b in anchors if a != b}
            edge_query('directed_pairs', lambda e, pairs=pairs: (e.from_node_id, e.to_node_id) in pairs,
                       'All directed assertions on the explicitly checked endpoint pairs.',
                       pairs=[list(pair) for pair in sorted(pairs)])
        elif kind == 'same_role_neighbors':
            owners = {edges[e].from_node_id for e in nominated if e in edges}
            owners |= set(check.get('node_ids', ()))
            predicates = {edges[e].edge_type for e in nominated if e in edges}
            edge_query('same_role_members', lambda e, owners=owners, predicates=predicates: e.from_node_id in owners and e.edge_type in predicates,
                       'All outgoing assertions for these owners and nominated predicates.',
                       owner_ids=sorted(owners), predicates=sorted(predicates))
        elif kind == 'related_decisions':
            rules = {key for key in anchors if key in nodes and nodes[key].node_type == 'generation_rule'}
            edge_query('rule_scope', lambda e, rules=rules: e.from_node_id in rules or e.to_node_id in rules,
                       'All assertions directly expressing the checked Rules\' operands, scope or consumers; operand neighborhoods are excluded.',
                       rule_ids=sorted(rules), direction='both')
        elif kind == 'binding_contest':
            physical = check.get('physical_id')
            bindings = edge_query('physical_owners', lambda e, physical=physical: e.edge_type == 'grounded_on'
                and e.to_node_id == physical and e.from_node_id in nodes and nodes[e.from_node_id].node_type == 'concept',
                'All Concept grounding claims on the contested physical target.', physical_id=physical)
            # A resolved check retains its explicitly nominated former owners.
            # Their grounding may be removed precisely because a preserved
            # Concept-to-Concept successor now expresses that representation.
            # Receipt premises must still include that successor afterwards.
            owners = {edges[e].from_node_id for e in bindings}
            owners |= {ident for ident in anchors if ident in nodes and nodes[ident].node_type == 'concept'}
            core |= owners
            edge_query('claimant_relations', lambda e, owners=owners: e.from_node_id in owners and e.to_node_id in owners,
                       'All business assertions between explicitly nominated or currently competing Concept owners, including preserved successors after a binding repair.', owner_ids=sorted(owners))
        elif kind == 'value_concepts':
            column = check.get('column_id')
            values = node_query('column_values', [n.id for n in nodes.values() if n.node_type == 'dimension_value'
                and n.properties.get('column_ref') == column], 'All catalog values belonging to the nominated column.', column_id=column)
            physical = values | ({column} if column else set())
            bindings = edge_query('value_and_column_bindings', lambda e, physical=physical: e.edge_type == 'grounded_on'
                and e.to_node_id in physical and e.from_node_id in nodes and nodes[e.from_node_id].node_type == 'concept',
                'All Concept bindings to the nominated column and its catalog values.', physical_ids=sorted(physical))
            concepts = {edges[e].from_node_id for e in bindings}
            concepts |= node_query('column_concepts', [n.id for n in nodes.values() if n.node_type == 'concept'
                and column in references(n)], 'All Concepts whose matching content references the nominated column.', column_id=column)
            core |= physical | concepts
            edge_query('value_concept_relations', lambda e, concepts=concepts: e.from_node_id in concepts and e.to_node_id in concepts,
                       'All business assertions between the selected value and column Concepts.', concept_ids=sorted(concepts))
        elif kind == 'source_selection_variants':
            concepts = set(check.get('node_ids', ()))
            consumers = {e.from_node_id for e in edges.values() if e.to_node_id in concepts and e.edge_type in {'depends_on', 'applies_to'}}
            consumers |= node_query('property_consumers', [n.id for n in nodes.values()
                if n.node_type in {'metric', 'generation_rule'} and references(n) & concepts],
                'All direct Metric/Rule property consumers of the selected Concepts.', consumed_ids=sorted(concepts))
            node_query('direct_consumers', consumers, 'All direct dependency or scope consumers of the selected Concepts.', consumed_ids=sorted(concepts))
            # A consumer's own roles/conditions can determine compatibility;
            # the bodies at their endpoints do not acquire adjacency permission.
            edge_query('consumer_scope', lambda e, consumers=consumers: e.from_node_id in consumers or e.to_node_id in consumers,
                       'All direct input, role, scope and consumer assertions of the affected Metrics/Rules; their other endpoint neighborhoods are excluded.',
                       consumer_ids=sorted(consumers), direction='both')
            edge_query('selected_concept_bindings', lambda e, concepts=concepts: e.edge_type == 'grounded_on' and e.from_node_id in concepts,
                       'All direct physical bindings of the Concepts under source-selection review.', concept_ids=sorted(concepts))
            core |= consumers
        elif kind == 'abstract_predicate':
            pairs = {(edges[e].from_node_id, edges[e].to_node_id) for e in nominated if e in edges}
            edge_query('abstract_role_pairs', lambda e, pairs=pairs: (e.from_node_id, e.to_node_id) in pairs,
                       'All assertions on the nominated abstract-role endpoint pairs.', pairs=[list(pair) for pair in sorted(pairs)])
        elif kind == 'metric_direct_inputs':
            metrics = {ident for ident in core if ident in nodes and nodes[ident].node_type == 'metric'}
            edge_query('metric_direct_inputs', lambda e, metrics=metrics: e.from_node_id in metrics and e.edge_type in {'depends_on', 'grounded_on'},
                       'All direct semantic and physical inputs of the nominated Metrics.', metric_ids=sorted(metrics))
        elif kind == 'target_alternative_scope':
            targets = {ident for ident in core if ident in nodes and nodes[ident].alternatives}
            edge_query('target_consumers', lambda e, targets=targets: e.to_node_id in targets,
                       'All current assertions consuming the explicitly nominated competing-version targets.', target_ids=sorted(targets))
        else:
            # Explicit followups identify their relations. Include other
            # assertions on exactly those endpoint pairs to expose successors.
            pairs = {(edges[e].from_node_id, edges[e].to_node_id) for e in nominated if e in edges}
            edge_query('obligation_pairs', lambda e, pairs=pairs: (e.from_node_id, e.to_node_id) in pairs,
                       'All assertions on the relation pairs named by the explicit obligation.', pairs=[list(pair) for pair in sorted(pairs)])
        scope.core_node_ids |= core
        scope.node_ids |= core

    # Core matching/formula references are required meanings, not anchors.
    scope.node_ids |= {ref for ident in scope.core_node_ids if ident in nodes for ref in references(nodes[ident]) if ref in nodes}
    scope.node_ids |= endpoints(scope.edge_ids)
    # Preserve only genuinely coupled metadata groups, not every group on a
    # boundary owner. Multiple groups can be linked by a shared real edge.
    coupled_owners = set()
    while True:
        old = len(scope.edge_ids)
        for ident, node in nodes.items():
            for group in _groups(node):
                if group & scope.edge_ids:
                    coupled_owners.add(ident)
                    scope.edge_ids |= group
        if old == len(scope.edge_ids):
            break
    scope.node_ids |= coupled_owners | endpoints(scope.edge_ids)
    scope.queries.append({'kind': 'metadata_consumers', 'edge_ids': sorted(scope.edge_ids),
                          'node_ids': sorted(coupled_owners),
                          'description': 'Complete metadata groups that reference any selected relationship, including linked groups.'})
    scope.writable_node_ids = {ident for ident in scope.core_node_ids | coupled_owners if ident in nodes and nodes[ident].node_type not in PHYSICAL}
    scope.metadata_only_node_ids = coupled_owners - scope.core_node_ids
    scope.writable_edge_ids = scope.edge_ids & edges.keys()
    contracts = effective_schema(context.schema, current.graph).edge_type_index()
    scope.contract_ids |= {edges[key].edge_type for key in scope.edge_ids if key in edges}
    for check in checks:
        if check.get('kind') != 'abstract_predicate':
            continue
        nominated = {key for key in check.get('edge_ids', ()) if key in edges}
        from_types = {nodes[edges[key].from_node_id].node_type for key in nominated}
        to_types = {nodes[edges[key].to_node_id].node_type for key in nominated}
        authorable = authoring_schema(context.schema, current.graph)
        candidates = {contract.id for contract in authorable.edge_types
            if any(authorable.is_node_type_compatible(t, a) for t in from_types for a in contract.from_node_types)
            and any(authorable.is_node_type_compatible(t, a) for t in to_types for a in contract.to_node_types)}
        scope.contract_ids |= candidates
        scope.queries.append({'kind': 'predicate_catalog', 'from_node_types': sorted(from_types),
                              'to_node_types': sorted(to_types), 'contract_ids': sorted(candidates),
                              'description': 'All authorable predicates compatible with the nominated endpoint types.'})
    pending = list(scope.contract_ids)
    while pending:
        contract = contracts.get(pending.pop())
        if contract and contract.extends and contract.extends not in scope.contract_ids:
            scope.contract_ids.add(contract.extends)
            pending.append(contract.extends)
    # Exact deterministic query deduplication keeps mixed-signal batching cheap.
    unique = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in scope.queries}
    scope.queries = [unique[key] for key in sorted(unique)]
    return scope
