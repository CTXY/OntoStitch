"""Controller-owned input units and the single public knowledge-routing contract."""
from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from cortex.capabilities.graph_merge.models import Model
from cortex.capabilities.graph_merge.runtime.state import canonical_hash


class SourceDestination(Model):
    kind: Literal['result', 'alternatives', 'common', 'discard']
    alternative_ids: tuple[str, ...] = ()

    @model_validator(mode='after')
    def coherent(self):
        if (self.kind == 'alternatives') != bool(self.alternative_ids):
            raise ValueError('Only alternatives destinations name one or more alternative_ids')
        if len(set(self.alternative_ids)) != len(self.alternative_ids):
            raise ValueError('Destination alternative IDs must be distinct')
        return self


class SourceRoute(Model):
    source_unit_id: str = Field(min_length=1, description='Copy an exact controller-provided source_units.unit_id; do not invent node/null scopes.')
    destination: SourceDestination
    explanation: str = Field(min_length=1)


class PatchContractError(ValueError):
    def __init__(self, code, *, owner_id=None, path=None, **details):
        self.details = {'code': code, 'owner_id': owner_id, 'path': path, **details}
        super().__init__(str(self.details))


def source_units(base, owner_id, member_ids):
    """Return real scopes, binding every handle to this accepted graph and owner.

    Structural common fields never create an extra source unit. Full normal
    content is a unit even when its provenance is empty; semantic preservation
    does not depend on the number of stored evidence references.
    """
    nodes = {n.id: n for n in base.graph.nodes}
    base_hash = canonical_hash(base)
    result = []
    for key in sorted(set(member_ids)):
        if key not in nodes:
            raise PatchContractError('unknown_source_member', owner_id=owner_id, member_id=key)
        node = nodes[key]
        if node.node_type in {'table', 'column', 'dimension_value'}:
            raise PatchContractError('physical_source_owner_readonly', owner_id=owner_id, node_id=key,
                required_action='Physical catalog objects are readonly; source units belong to semantic owners.')
        scopes = [('alternative', a.alternative_id, a.sources) for a in node.alternatives]
        if not node.alternatives:
            scopes.append(('normal_content', None, node.sources))
        elif node.common_sources:
            scopes.append(('common', None, node.common_sources))
        for kind, alternative_id, sources in scopes:
            token = canonical_hash([base_hash, owner_id, key, kind, alternative_id])
            result.append({'unit_id': 'source::' + token[:24], 'node_id': key, 'kind': kind,
                           **({'alternative_id': alternative_id} if alternative_id else {}),
                           '_sources': tuple(sources)})
    return result


def source_unit_manifest(base, groups=None, owner_ids=()):
    """Compact model-facing operation/scopes for identity groups and single owners."""
    groups = list(groups or [])
    grouped = {key for g in groups for key in g['member_ids']}
    rows = [{'owner_id': g['owner_id'], 'member_ids': list(g['member_ids']),
             'required_operation': 'integrate_groups'} for g in groups]
    rows.extend({'owner_id': key, 'member_ids': [key], 'required_operation': 'conflicts_or_resolutions'}
                for key in sorted(set(owner_ids) - grouped))
    for row in rows:
        row['source_units'] = [{k: v for k, v in u.items() if k != '_sources'}
                               for u in source_units(base, row['owner_id'], row['member_ids'])]
    return rows


def validate_source_routes(base, integration, *, units=None):
    # A local partition keeps handles from the fixed INPUT manifest, even when
    # an output subgroup has a different owner. Only the controller supplies it.
    units = source_units(base, integration.owner_id, integration.member_ids) if units is None else units
    index = {u['unit_id']: u for u in units}
    counts = Counter(r.source_unit_id for r in integration.source_routes)
    missing, extra = sorted(index.keys() - counts.keys()), sorted(counts.keys() - index.keys())
    duplicates = sorted(key for key, n in counts.items() if n > 1)
    if missing or extra or duplicates:
        raise PatchContractError('source_routes_mismatch', owner_id=integration.owner_id,
            path='source_routes', missing=missing, extra=extra, duplicates=duplicates,
            required_source_units=[{k: v for k, v in u.items() if k != '_sources'} for u in units],
            required_action='Route every listed input unit exactly once; remove extra and duplicate routes.')
    alternative_ids = {a.alternative_id for a in integration.alternatives}
    routes = {}
    for i, route in enumerate(integration.source_routes):
        unit = index[route.source_unit_id]
        kind, dest = route.destination.kind, set(route.destination.alternative_ids)
        error = None
        if kind == 'discard' and unit['kind'] != 'alternative':
            error = 'Only an old alternative may be explicitly discarded; normal/common input knowledge must survive.'
        elif kind in {'common', 'alternatives'} and not alternative_ids:
            error = 'A normal result uses result destinations, not common/alternatives.'
        elif kind == 'result' and alternative_ids:
            error = 'A conflict result uses common or named alternatives, not result.'
        elif dest - alternative_ids:
            error = 'Destination alternative IDs must exist in this complete result.'
        if error:
            raise PatchContractError('invalid_source_destination', owner_id=integration.owner_id,
                path=f'source_routes[{i}].destination', source_unit_id=route.source_unit_id,
                problem=error, allowed_alternative_ids=sorted(alternative_ids))
        routes[(unit['node_id'], unit.get('alternative_id'))] = (unit, route)
    return routes
