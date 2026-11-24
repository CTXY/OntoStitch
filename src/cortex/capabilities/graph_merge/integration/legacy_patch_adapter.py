"""Explicit offline v2 Patch replay adapter; never used by model submission.

Unknown old scopes are preserved as invalid route IDs, not silently repaired.
This allows saved failed proposals to exercise precise v3 diagnostics.
"""
from copy import deepcopy

from cortex.capabilities.graph_merge.graph.source_units import source_units


def convert_legacy_patch(raw, base):
    data = deepcopy(raw)
    nodes = {n.id: n for n in base.graph.nodes}
    for operation in ('integrate_groups', 'conflicts'):
        for group in data.get(operation, []):
            owner = group.get('owner_id', group.get('node_id'))
            members = group.get('member_ids', [owner])
            if owner not in nodes:
                group['source_routes'] = []
                group.pop('branch_dispositions', None)
                continue
            units = source_units(base, owner, members)
            lookup = {(u['node_id'], u.get('alternative_id')): u for u in units}
            if 'source_routes' in group:
                continue
            routes = []
            normal = bool(group.get('content'))
            for old in group.pop('branch_dispositions', []):
                key = (old['source_node_id'], old.get('source_alternative_id'))
                unit = lookup.get(key)
                unit_id = unit['unit_id'] if unit else f'legacy-unlisted::{key[0]}::{key[1]}'
                if old.get('retained_in_common'):
                    dest = {'kind': 'result' if normal else 'common'}
                elif old.get('target_alternative_ids'):
                    dest = {'kind': 'alternatives', 'alternative_ids': old['target_alternative_ids']}
                else:
                    dest = {'kind': 'discard'}
                routes.append({'source_unit_id': unit_id, 'destination': dest,
                               'explanation': old['explanation']})
            # V2 automatically retained these scopes in a normal result. Make
            # that historical behavior explicit; do not invent missing branches.
            if normal:
                supplied = {r['source_unit_id'] for r in routes}
                routes.extend({'source_unit_id': u['unit_id'], 'destination': {'kind': 'result'},
                               'explanation': 'V2 normal integration automatically retained this input knowledge.'}
                              for u in units if u['kind'] != 'alternative' and u['unit_id'] not in supplied)
            group['source_routes'] = routes
    return data
