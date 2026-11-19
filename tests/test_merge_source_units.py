"""One explicit source-routing contract, including portable real failed proposals."""
from copy import deepcopy
import gzip
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from test_graph_integration import catalog, concept, state
from test_merge_identity_integration_v2 import conflicted, content, points, prepare
from cortex.capabilities.graph_merge.integration.compiler import Preview
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.legacy_patch_adapter import convert_legacy_patch
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import IntegrationProposal
from cortex.capabilities.graph_merge.integration.patch import MergePatch, compile_patch, identity_obligations
from cortex.capabilities.graph_merge.integration.provenance import validate_rewrite_sources
from cortex.capabilities.graph_merge.integration.source_units import PatchContractError, source_unit_manifest
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import GraphSpace, GraphState, load_root_schema


FIXTURE = Path(__file__).parent / 'fixtures/thrombosis_contract_failure'


def fixture(name):
    return json.loads(gzip.decompress((FIXTURE / name).read_bytes()))


def simple_patch(base):
    units = source_unit_manifest(base, [{'owner_id': 'A', 'member_ids': ['A', 'B']}])[0]['source_units']
    return {'integrate_groups': [{'owner_id': 'A', 'member_ids': ['A', 'B'],
        'content': content(next(n for n in base.graph.nodes if n.id == 'A')),
        'source_routes': [{'source_unit_id': u['unit_id'], 'destination': {'kind': 'result'},
                           'explanation': 'Preserve this accepted meaning.'} for u in units],
        'explanation': 'Integrate both members.'}], 'explanation': 'One complete fixed-base draft.'}


def test_public_schema_has_one_route_protocol_and_no_internal_source_scopes():
    schema = json.dumps(MergePatch.model_json_schema())
    assert 'source_routes' in schema
    assert 'branch_dispositions' not in schema
    assert 'source_alternative_id' not in schema
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a')])
    raw = simple_patch(base)
    raw['integrate_groups'][0]['branch_dispositions'] = []
    with pytest.raises(ValidationError, match='Extra inputs'):
        MergePatch.model_validate(raw)


def test_manifest_is_exact_base_bound_and_stable_for_reordered_members():
    owner = conflicted('A', ['left', 'right'])
    base = state([*catalog(), owner, concept('B', 'a')])
    get = lambda members: source_unit_manifest(base, [{'owner_id': 'A', 'member_ids': members}])[0]['source_units']
    units = get(['A', 'B'])
    assert units == get(['B', 'A'])
    assert len(units) == 3
    assert not any(u['node_id'] == 'A' and u['kind'] == 'common' for u in units)
    changed = base.model_copy(update={'graph': base.graph.model_copy(update={'nodes': tuple(
        n.model_copy(update={'name': 'A different accepted baseline'}) if n.id == 'B' else n
        for n in base.graph.nodes)})})
    newer = source_unit_manifest(changed, [{'owner_id': 'A', 'member_ids': ['A', 'B']}])[0]['source_units']
    assert {u['unit_id'] for u in units}.isdisjoint(u['unit_id'] for u in newer)


def test_routes_report_missing_extra_and_duplicate_without_mutating_base():
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a')]);ctx, _ = prepare(base)
    ctx.candidate = base
    raw = simple_patch(base);routes = raw['integrate_groups'][0]['source_routes']
    missing = routes.pop()['source_unit_id'];routes.append(deepcopy(routes[0]))
    routes.append({'source_unit_id': 'invented', 'destination': {'kind': 'result'}, 'explanation': 'Invalid.'})
    with pytest.raises(PatchContractError) as caught:
        compile_patch(ctx, base, MergePatch.model_validate(raw), targets={'A', 'B'}, aliases={'A': 'A', 'B': 'A'})
    error = caught.value.details
    assert error['missing'] == [missing] and error['extra'] == ['invented']
    assert error['duplicates'] == [routes[0]['source_unit_id']]
    assert ctx.candidate is base


@pytest.mark.parametrize('target', ['A', 'B'])
@pytest.mark.parametrize('operation', ['conflicts', 'resolutions'])
def test_single_owner_operations_never_replace_a_planned_member_group(target, operation):
    base = state([*catalog(), conflicted('A', ['one', 'two']), conflicted('B', ['one', 'two'])]);ctx, _ = prepare(base)
    if operation == 'resolutions':
        raw = {operation: [{'node_id': target, 'keep_alternative_id': 'one', 'discard_reasons': {'two': 'Explicit correction.'}}]}
    else:
        node = next(n for n in base.graph.nodes if n.id == target)
        from cortex.application.ontology_graph import GraphManager
        raw = {operation: [{'node_id': target, 'alternatives': [{'alternative_id': a.alternative_id,
            'content': GraphManager.resolve_alternative(node, a).model_dump(mode='json'), 'evidence_ids': []}
            for a in node.alternatives], 'conflict_points': points(['one', 'two'])}]}
    raw['explanation'] = 'Cannot bypass owed identity integration.'
    with pytest.raises(PatchContractError, match='identity_operation_required'):
        compile_patch(ctx, base, MergePatch.model_validate(raw), targets={'A', 'B'}, aliases={'A': 'A', 'B': 'A'})


def replay_context():
    base = GraphState.model_validate(fixture('accepted_base.json.gz'));task = fixture('open_task.json.gz')
    sources = {s for n in base.graph.nodes for s in n.sources} | {s for e in base.graph.edges for s in e.sources}
    evidence = tuple(EvidenceItem(id='sql-training:' + s.source_id, source=s, case_id=s.case_id, content={}) for s in sources)
    context = IntegrationContext(base, GraphState(graph=GraphSpace()), load_root_schema(), evidence=evidence)
    context.candidate = base
    context.task_visible_ids = {n.id for n in base.graph.nodes} | {e.id for e in base.graph.edges}
    payload = {'candidate_nodes': [n.model_dump(mode='json') for n in base.graph.nodes],
               'candidate_relations': [e.model_dump(mode='json') for e in base.graph.edges]}
    return base, task, context, payload


@pytest.mark.parametrize('index', range(5))
def test_real_failed_patch_reports_exact_contract_failure(index):
    base, task, ctx, payload = replay_context();entry = fixture('failed_patches.json.gz')[index]
    raw = convert_legacy_patch(entry['patch'], base)
    with pytest.raises(PatchContractError) as caught:
        compile_patch(ctx, base, MergePatch.model_validate(raw), targets=set(task['targets']), aliases=task['aliases'], payload=payload)
    details = caught.value.details
    if index in {0, 1, 3}:
        assert details['code'] == 'source_routes_mismatch'
        assert details['missing'] == [] and details['duplicates'] == []
        assert details['extra'] == ['legacy-unlisted::incoming.concept.41452826bf4c080199e2::None']
    else:
        assert details['code'] == 'identity_operation_required'
    assert ctx.candidate is base


def test_minimally_corrected_real_patch_compiles_all_groups_and_preserves_sources():
    base, task, ctx, payload = replay_context();raw = convert_legacy_patch(fixture('failed_patches.json.gz')[0]['patch'], base)
    for group in raw['integrate_groups']:
        group['source_routes'] = [r for r in group['source_routes'] if not r['source_unit_id'].startswith('legacy-unlisted::')]
    # An integrated owner's name belongs in its complete result contents; the
    # extra owner CRUD rename is a second contradictory owner operation.
    raw['node_updates'] = []
    compiled = compile_patch(ctx, base, MergePatch.model_validate(raw), targets=set(task['targets']), aliases=task['aliases'], payload=payload)
    assert len(raw['integrate_groups']) == len(identity_obligations(base, task['aliases'])) == 14
    remaining = {n.id for n in compiled.state.graph.nodes}
    assert all(key == owner or key not in remaining for key, owner in task['aliases'].items())
    lineage = LineageIndex(ctx)
    lineage.observe(base, compiled.integrated, compiled.integration_changes,
                    node_aliases=compiled.node_aliases, edge_aliases=compiled.edge_aliases)
    lineage.observe(compiled.integrated, compiled.state, compiled.changes)
    validate_rewrite_sources(ctx, IntegrationProposal(changes=lineage.source_changes(compiled.state)),
                            Preview(compiled.state, lineage.node_map, lineage.edge_map, []))


def test_source_less_old_alternatives_can_be_retained_without_inventing_evidence():
    from cortex.application.ontology_graph import GraphManager
    old = conflicted('A', ['one', 'two'])
    owner, _ = GraphManager.compact_conflict(old, [(a.alternative_id,
        GraphManager.resolve_alternative(old, a), ()) for a in old.alternatives], conflict_points=old.conflict_points)
    base = state([*catalog(), owner]);ctx, _ = prepare(base)
    units = source_unit_manifest(base, owner_ids=['A'])[0]['source_units']
    raw = {'conflicts': [{'node_id': 'A', 'alternatives': [
        {'alternative_id': a.alternative_id, 'content': GraphManager.resolve_alternative(owner, a).model_dump(mode='json')}
        for a in owner.alternatives], 'conflict_points': points(['one', 'two']),
        'source_routes': [{'source_unit_id': u['unit_id'], 'destination': {
            'kind': 'alternatives', 'alternative_ids': [u['alternative_id']]},
            'explanation': 'Retain this exact historical content without fabricated evidence.'} for u in units]}],
        'explanation': 'Existing historical source-less branches remain representable.'}
    compiled = compile_patch(ctx, base, MergePatch.model_validate(raw), targets={'A'},
                             payload={'candidate_nodes': [owner.model_dump(mode='json')]})
    current = next(n for n in compiled.state.graph.nodes if n.id == 'A')
    assert current.sources == ()
    assert [GraphManager.resolve_alternative(current, a) for a in current.alternatives] == [
        GraphManager.resolve_alternative(owner, a) for a in owner.alternatives]


def test_normal_input_cannot_be_discarded_even_with_an_explanation():
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a')]);ctx, _ = prepare(base)
    raw = simple_patch(base)
    raw['integrate_groups'][0]['source_routes'][0]['destination'] = {'kind': 'discard'}
    with pytest.raises(PatchContractError, match='invalid_source_destination'):
        compile_patch(ctx, base, MergePatch.model_validate(raw), targets={'A', 'B'}, aliases={'A': 'A', 'B': 'A'})


def test_conflict_and_resolution_cannot_both_own_a_standalone_node():
    from cortex.application.ontology_graph import GraphManager
    owner = conflicted('A', ['one', 'two']);base = state([*catalog(), owner]);ctx, _ = prepare(base)
    raw = {'conflicts': [{'node_id': 'A', 'alternatives': [{'alternative_id': a.alternative_id,
        'content': GraphManager.resolve_alternative(owner, a).model_dump(mode='json')}
        for a in owner.alternatives], 'conflict_points': points(['one', 'two'])}],
        'resolutions': [{'node_id': 'A', 'keep_alternative_id': 'one', 'discard_reasons': {'two': 'Correction.'}}],
        'explanation': 'Conflicting owner operations are not sequential instructions.'}
    with pytest.raises(PatchContractError, match='overlapping_owner_operations'):
        compile_patch(ctx, base, MergePatch.model_validate(raw), targets={'A'})
