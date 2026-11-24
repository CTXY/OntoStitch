"""Deterministic, nonrecursive premises for one reconciliation check.

Queries describe complete sets, including empty sets: a newly arriving edge,
owner, consumer or catalog value changes a premise without a previous object ID.
Boundary node bodies are included; their unrelated adjacency is never expanded.
No task/batch ID, workspace revision, global graph hash or policy enters here.
"""
import json
from copy import deepcopy

from cortex.capabilities.graph_merge.graph.operations import PHYSICAL
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.schema.relation_contracts import effective_schema

VERSION = 2
_SET_FIELDS = {'node_ids', 'edge_ids', 'owner_ids', 'member_ids', 'anchor_ids',
               'consumer_ids', 'shared_operands', 'members', 'variants', 'evidence_ids'}
_CATALOG_FIELDS = {'table_name', 'column_name', 'table_ref', 'column_ref', 'value',
                   'data_type', 'nullable', 'semantic_role', 'key_role', 'unit'}


def _sorted(values):
    return sorted(values, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))


def normalize_signal(value, key=None):
    """Canonicalize declared sets, preserving ordered business lists/expressions."""
    if isinstance(value, dict):
        return {k: normalize_signal(v, k) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple, set)):
        rows = [normalize_signal(row) for row in value]
        return _sorted(rows) if key in _SET_FIELDS or isinstance(value, set) else rows
    return value


def signal_key(check):
    if not isinstance(check, dict) or not isinstance(check.get('kind'), str):
        raise ValueError('Signal requires a kind')
    ident = check.get('id') or check.get('signal_id')
    if ident is not None and (not isinstance(ident, str) or not ident.strip()):
        raise ValueError('Signal ID must be a nonempty string')
    return ident or check['kind'] + ':' + canonical_hash(normalize_signal(check))


def premise_object(obj):
    """Canonical complete business/attribution body shared by explicit reads.

    Source references and complete competing versions remain intact. Only set
    ordering, internal origin locators and non-public physical catalog metadata
    are excluded; ordered matching/expression lists are deliberately preserved.
    """
    row = obj.model_dump(mode='json', exclude={'reference_origin'})
    for field in ('sources', 'common_sources', 'aliases', 'tags'):
        if field in row:
            row[field] = _sorted(row[field])
    if 'alternatives' in row:
        for alt in row['alternatives']:
            alt['sources'] = _sorted(alt.get('sources', []))
        row['alternatives'] = sorted(row['alternatives'], key=lambda a: a['alternative_id'])
    if 'alternative_ids' in row.get('properties', {}):
        row['properties']['alternative_ids'] = sorted(row['properties']['alternative_ids'])
    if row.get('node_type') in PHYSICAL:
        row['properties'] = {k: v for k, v in row['properties'].items() if k in _CATALOG_FIELDS}
    return row


def signal_premise(context, current, check):
    """Return the same exact scope the Writer/Assessment receive, plus evidence."""
    from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope
    signal_key(check)
    nodes = {n.id: n for n in current.graph.nodes}
    edges = {e.id: e for e in current.graph.edges}
    scope = signal_scope(context, current, [check])
    selected_nodes, selected_edges = scope.node_ids, scope.edge_ids
    explicit_contracts, queries = scope.contract_ids, scope.queries
    contracts = effective_schema(context.schema, current.graph).edge_type_index()
    bodies = [nodes[key] for key in sorted(selected_nodes) if key in nodes]
    relations = [edges[key] for key in sorted(selected_edges) if key in edges]
    refs = {source for obj in (*bodies,*relations) for source in obj.sources}
    refs |= {source for node in bodies for source in node.common_sources}
    refs |= {source for node in bodies for alt in node.alternatives for source in alt.sources}
    evidence = [{'id':e.id,'source':e.source.model_dump(mode='json'),'content':deepcopy(e.content)}
                for e in context.evidence if e.source in refs]
    return {'version':VERSION,'signal':normalize_signal(check),'sql_dialect':getattr(context,'sql_dialect',None),
            'queries':_sorted(queries),'nodes':[premise_object(n) for n in bodies],
            'edges':[premise_object(e) for e in relations],
            'missing_node_ids':sorted(selected_nodes - nodes.keys()),
            'missing_edge_ids':sorted(selected_edges - edges.keys()),
            'contracts':[contracts[key].model_dump(mode='json') for key in sorted(explicit_contracts) if key in contracts],
            'missing_contract_ids':sorted(explicit_contracts-contracts.keys()),
            'evidence':_sorted(evidence)}
