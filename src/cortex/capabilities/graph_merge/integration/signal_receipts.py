"""Controller-owned, per-check reconciliation acceptance receipts.

Batch grouping and dialogue IDs are deliberately absent from the premise. Actual
supplementary reads remain conservative dependencies of the accepted judgment.
"""
from copy import deepcopy

from cortex.capabilities.graph_merge.integration.task_state import canonical_hash


def policy_hash(engine):
    versions = engine.contract_versions
    return canonical_hash({'receipt_protocol': 1, 'contracts': versions.to_dict()})


def read_dependencies(state):
    rows = {}
    for record in state.agent_recovery.values():
        for row in record.get('runtime', {}).get('journal', []):
            if row.get('name') in {'read_nodes', 'read_relations', 'read_evidence', 'read_predicates', 'counterexample'}:
                # Arguments contain complete-resource queries, not historical
                # tool responses; recompute them against accepted current.
                row = deepcopy(row)
                for key in ('ids', 'node_ids'):
                    if key in row['args']:
                        row['args'][key] = sorted(set(row['args'][key]))
                rows[canonical_hash(row)] = row
    return [rows[key] for key in sorted(rows)]


def dependency_facts(context, current, queries):
    from cortex.schema.relation_contracts import authoring_schema, effective_schema
    from cortex.capabilities.graph_merge.integration.signal_premises import premise_object
    nodes = {n.id: n for n in current.graph.nodes}
    edges = {e.id: e for e in current.graph.edges}
    evidence = {e.id: e.model_dump(mode='json') for e in context.evidence}
    contracts = effective_schema(context.schema, current.graph).edge_type_index()
    facts = []
    for query in queries:
        name, args = query['name'], query['args']
        row = {'query': query}
        if name == 'read_nodes':
            row['nodes'] = {i: premise_object(nodes[i]) if i in nodes else None for i in sorted(args['ids'])}
        elif name == 'read_relations':
            if 'checks' in args:
                from cortex.capabilities.graph_merge.integration.signal_scope import signal_scope
                scope = signal_scope(context, current, args['checks'])
                ids = set(scope.node_ids)
                chosen = [e for e in edges.values() if e.id in scope.edge_ids]
                row['relation_sets'] = scope.queries
            else:
                # Historical receipts retain their original conservative query.
                ids = set(args['node_ids'])
                chosen = [e for e in edges.values() if e.from_node_id in ids or e.to_node_id in ids]
            row['edges'] = [premise_object(e) for e in sorted(chosen, key=lambda e: e.id)]
            ids |= {i for e in chosen for i in (e.from_node_id, e.to_node_id)}
            row['nodes'] = {i: premise_object(nodes[i]) if i in nodes else None for i in sorted(ids)}
        elif name == 'read_evidence':
            row['evidence'] = {i: evidence.get(i) for i in sorted(args['ids'])}
        elif name == 'read_predicates':
            schema = authoring_schema(context.schema, current.graph)
            row['contracts'] = [c.model_dump(mode='json') for c in sorted(schema.edge_types, key=lambda c: c.id)
                if any(schema.is_node_type_compatible(args['from_node_type'], k) for k in c.from_node_types)
                and any(schema.is_node_type_compatible(args['to_node_type'], k) for k in c.to_node_types)]
        elif name == 'counterexample':
            row['edges'] = [premise_object(e) for e in sorted(edges.values(), key=lambda e: e.id)
                if (not args.get('from_node_id') or e.from_node_id == args['from_node_id'])
                and e.to_node_id == args['to_node_id'] and e.edge_type == args['edge_type']
                and (args.get('kind') != 'concept_owner_absent' or nodes[e.from_node_id].node_type == 'concept')
                and (not args.get('alternative_id') or not e.properties.get('alternative_ids')
                     or args['alternative_id'] in e.properties['alternative_ids'])]
        else:
            raise ValueError('Unsupported receipt dependency query')
        # Predicate meaning and supporting evidence are part of explicit reads.
        used_edges = row.get('edges', [])
        row['used_contracts'] = {e['edge_type']: contracts[e['edge_type']].model_dump(mode='json')
            for e in used_edges if e['edge_type'] in contracts}
        objects = [*used_edges, *(v for v in row.get('nodes', {}).values() if v is not None)]
        refs = {canonical_hash(s) for obj in objects for s in obj.get('sources', [])}
        refs |= {canonical_hash(s) for obj in objects for s in obj.get('common_sources', [])}
        refs |= {canonical_hash(s) for obj in objects for alt in obj.get('alternatives', []) for s in alt.get('sources', [])}
        row['support'] = [e.model_dump(mode='json') for e in sorted(context.evidence, key=lambda e: e.id)
            if canonical_hash(e.source.model_dump(mode='json')) in refs]
        facts.append(row)
    return facts


def premise_hash(engine, check, dependencies=()):
    from cortex.capabilities.graph_merge.integration.signal_premises import signal_premise
    return canonical_hash({'check': signal_premise(engine.ctx, engine.current, check),
        'supplementary': dependency_facts(engine.ctx, engine.current, dependencies)})


def is_accepted(engine, check):
    from cortex.capabilities.graph_merge.integration.receipt_ledger import latest_receipt, has_receipt
    policy = policy_hash(engine)
    ledger = getattr(engine, 'signal_receipts', [])
    record = latest_receipt(ledger, check, policy_hash=policy)
    if record is None:
        from cortex.capabilities.graph_merge.integration.signal_scope_migration import migrated_receipt
        return migrated_receipt(engine, check) is not None
    return has_receipt(ledger, check, premise_hash=premise_hash(engine, check, record['dependencies']), policy_hash=policy)


def record_acceptance(engine, task, state, history_entry):
    from cortex.capabilities.graph_merge.integration.receipt_ledger import make_receipt
    if task.phase != 'reconcile' or not getattr(engine.agent, 'reconciliation_contract', False):
        return
    dependencies = read_dependencies(state)
    policy = policy_hash(engine)
    for check in task.checks:
        record = make_receipt(check, premise_hash=premise_hash(engine, check, dependencies),
            policy_hash=policy, history_entry=history_entry, candidate_hash=history_entry['candidate_hash'],
            dependencies=dependencies)
        engine.signal_receipts = [*getattr(engine, 'signal_receipts', []), record]


def stable_task_id(engine, checks):
    from cortex.capabilities.graph_merge.integration.receipt_ledger import latest_receipt
    from cortex.capabilities.graph_merge.integration.signal_premises import signal_key
    rows = []
    for c in checks:
        previous = latest_receipt(getattr(engine, 'signal_receipts', []), c, policy_hash=policy_hash(engine))
        if previous is None:
            from cortex.capabilities.graph_merge.integration.signal_scope_migration import migrated_receipt
            previous = migrated_receipt(engine, c)
        rows.append((signal_key(c), premise_hash(engine, c, previous['dependencies'] if previous else ())))
    rows.sort()
    return 'structure-' + canonical_hash({'checks': rows, 'policy': policy_hash(engine)})[:24]
