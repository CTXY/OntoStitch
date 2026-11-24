"""Independent permission and provenance checks for local group corrections."""
from copy import deepcopy

import pytest

from test_graph_integration import catalog, concept, edge, state
from test_merge_identity_integration_v2 import content, prepare

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.patch import MergePatch, compile_patch
from cortex.capabilities.graph_merge.llm.node_context import build_node_packet
from cortex.capabilities.graph_merge.llm.node_contract import normalize_node_result
from cortex.schema import SourceReference, load_root_schema


def fixture():
    relation = edge('inside', 'B', 'C', 'is_a').model_copy(update={
        'sources': (SourceReference(source_id='relation-only-source'),)})
    external = edge('external', 'A', 'OUT', 'is_a')
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a'), concept('C', 'a'),
                  concept('OUT', 'b')], [relation, external])
    ctx, _ = prepare(base)
    group = {'owner_id': 'A', 'member_ids': ['A', 'B', 'C']}
    ctx.task_before = base
    ctx.task_identity_groups = [group]
    ctx.node_local_compile = True
    packet = build_node_packet(ctx, {'phase': 'consolidate'})
    return base, ctx, group, packet


def result(group, packet, *, separate=(), removals=()):
    retained = [key for key in group['member_ids'] if key not in separate]
    revised = ({'owner_id': group['owner_id'] if group['owner_id'] in retained else retained[0], 'member_ids': retained}
               if len(retained) >= 2 else None)
    raw = {'keep_separate_ids': list(separate), 'integrate_groups': [],
           'relation_removals': [{'edge_id': ident, 'reason': 'The integrated identity retains this redundant assertion and its source.'}
                                 for ident in removals],
           'explanation': 'Correct this fixed local group only.'}
    if revised is not None:
        raw['integrate_groups'] = [{**revised,
            'content': content(concept(revised['owner_id'], 'a')),
            'source_routes': [{'source_unit_id': unit['unit_id'], 'destination': {'kind': 'result'},
                              'explanation': 'Retain this complete original member.'}
                             for unit in packet['source_unit_manifest'][0]['source_units']
                             if unit['node_id'] in revised['member_ids']],
            'explanation': 'Integrate exactly the retained members.'}]
    return raw


def compile_result(base, ctx, packet, raw, *, bypass_public=False):
    if bypass_public:
        patch = MergePatch.model_validate(raw)
    else:
        patch, _ = normalize_node_result('writer', raw, payload=packet, base=base)
    return compile_patch(ctx, base, patch, targets={'A'}, aliases={'A': 'A', 'B': 'A', 'C': 'A'})


def test_excluding_original_owner_preserves_it_and_uses_original_manifest_handles():
    base, ctx, group, packet = fixture()
    raw = result(group, packet, separate=['A'], removals=['inside'])
    original_handles = {row['unit_id'] for row in packet['source_unit_manifest'][0]['source_units']
                        if row['node_id'] in {'B', 'C'}}
    assert {row['source_unit_id'] for row in raw['integrate_groups'][0]['source_routes']} == original_handles
    saved_raw = deepcopy(raw)
    compiled = compile_result(base, ctx, packet, raw)
    GraphManager(load_root_schema()).validate_state(compiled.state)
    nodes = {n.id: n for n in compiled.state.graph.nodes}
    original_nodes = {n.id: n for n in base.graph.nodes}
    assert nodes['A'] == original_nodes['A'] and nodes['OUT'] == original_nodes['OUT']
    assert compiled.node_aliases == {'B': 'B', 'C': 'B'} and 'C' not in nodes
    assert SourceReference(source_id='relation-only-source') in nodes['B'].sources
    assert next(e for e in compiled.state.graph.edges if e.id == 'external') == next(e for e in base.graph.edges if e.id == 'external')
    assert raw == saved_raw


@pytest.mark.parametrize('separate', [('A', 'B', 'C')])
def test_all_or_one_remaining_member_preserves_every_original_node_and_relation(separate):
    base, ctx, group, packet = fixture()
    raw = result(group, packet, separate=separate)
    assert raw['integrate_groups'] == []
    compiled = compile_result(base, ctx, packet, raw)
    assert compiled.state == base and compiled.node_aliases == {}


@pytest.mark.parametrize('bypass_public', [False, True])
@pytest.mark.parametrize('removal', ['external', 'missing-edge'])
def test_relation_removal_never_escapes_original_internal_edges(bypass_public, removal):
    base, ctx, group, packet = fixture()
    raw = result(group, packet, separate=['C'], removals=[removal])
    before = base.model_dump_json()
    with pytest.raises(ValueError, match='node_relation_removal_outside_group'):
        compile_result(base, ctx, packet, raw, bypass_public=bypass_public)
    assert base.model_dump_json() == before


@pytest.mark.parametrize('bypass_public', [False, True])
def test_separation_cannot_name_an_unrelated_node_even_through_internal_patch(bypass_public):
    base, ctx, group, packet = fixture()
    raw = result(group, packet)
    raw['keep_separate_ids'] = ['OUT']
    with pytest.raises(ValueError, match='invalid_keep_separate_ids'):
        compile_result(base, ctx, packet, raw, bypass_public=bypass_public)


def test_internal_patch_cannot_retract_relation_with_blank_reason():
    base, ctx, group, packet = fixture()
    raw = result(group, packet, removals=['inside'])
    raw['relation_removals'][0]['reason'] = ' '
    with pytest.raises(ValueError):
        compile_result(base, ctx, packet, raw, bypass_public=True)


def test_removed_relation_cannot_also_be_queued_as_future_followup():
    base, _, group, packet = fixture()
    raw = result(group, packet, removals=['inside'])
    raw['relation_followups'] = [{'edge_ids': ['inside'], 'problem': 'Revisit this same edge.',
                                  'required_outcome': 'Resolve this same edge later.'}]
    with pytest.raises(ValueError):
        normalize_node_result('writer', raw, payload=packet, base=base)
