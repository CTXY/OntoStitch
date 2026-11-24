"""Local revision packets show all actual survivors without exposing the graph."""
from types import SimpleNamespace

from test_node_local_context import context

from cortex.capabilities.graph_merge.consolidation.presentation import NodeTaskDialogue, build_node_packet
from cortex.schema import GraphEdge, GraphNode


def test_assessment_shows_separate_original_owner_and_changed_integration_owner():
    ctx = context()
    third = GraphNode(id='N3', node_type='concept', name='Third original',
                      properties={'targets': [['b']]})
    third_edge = GraphEdge(id='e3', edge_type='grounded_on', from_node_id='N3', to_node_id='b',
                           properties={'description': 'Third original representation.'})
    ctx.task_before = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={
        'nodes': (*ctx.task_before.graph.nodes, third),
        'edges': (*ctx.task_before.graph.edges, third_edge)})})
    ctx.task_identity_groups[0]['member_ids'].append('N3')
    ctx.candidate = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={
        'nodes': tuple(n for n in ctx.task_before.graph.nodes if n.id != 'N3'),
        'edges': tuple(e.model_copy(update={'from_node_id': 'N2'}) if e.id == 'e3' else e
                       for e in ctx.task_before.graph.edges)})})
    ctx.active_draft = {'keep_separate_ids': ['N1'], 'integrate_groups': [
        {'owner_id': 'N2', 'member_ids': ['N2', 'N3'], 'source_routes': []}]}
    ctx.last_compiled_patch = SimpleNamespace(node_aliases={'N2': 'N2', 'N3': 'N2'}, edge_aliases={})
    packet = build_node_packet(ctx, {'phase': 'node_assess'})
    assert packet['candidate']['integrated_groups'][0]['owner_id'] == 'N2'
    assert packet['candidate']['singleton_ids'] == ['N1']
    assert {n['id'] for n in packet['candidate']['nodes']} == {'N1', 'N2'}
    assert len([n for n in packet['candidate']['nodes'] if n['id'] == 'N2']) == 1
    assert next(n for n in packet['candidate']['nodes'] if n['id'] == 'N2')['properties']
    assert {e['id'] for e in packet['candidate']['relations']} == {'e1', 'e2', 'e3'}
    assert {e['from_node_id'] for e in packet['candidate']['relations']} == {'N1', 'N2'}
    assert 'OUT' not in {n['id'] for n in packet['candidate']['nodes']}


def test_all_separate_members_remain_visible_with_their_relations():
    ctx = context()
    ctx.active_draft = {'keep_separate_ids': ['N1', 'N2'], 'integrate_groups': []}
    packet = build_node_packet(ctx, {'phase': 'node_assess'})
    assert {n['id'] for n in packet['candidate']['nodes']} == {'N1', 'N2'}
    assert {e['id'] for e in packet['candidate']['relations']} == {'e1', 'e2'}
    assert packet['candidate']['integrated_groups'] == []
    assert packet['candidate']['singleton_ids'] == ['N1', 'N2']


def test_standalone_candidate_content_changes_assessment_premise_hash():
    ctx = context()
    ctx.active_draft = {'keep_separate_ids': ['N1', 'N2'], 'integrate_groups': []}
    original = build_node_packet(ctx, {'phase': 'node_assess'})
    ctx.candidate = ctx.candidate.model_copy(update={'graph': ctx.candidate.graph.model_copy(update={
        'nodes': tuple(n.model_copy(update={'description': 'Changed standalone meaning.'}) if n.id == 'N2' else n
                       for n in ctx.candidate.graph.nodes)})})
    changed = build_node_packet(ctx, {'phase': 'node_assess'})
    assert original['candidate']['singleton_ids'] == changed['candidate']['singleton_ids'] == ['N1', 'N2']
    assert NodeTaskDialogue('fixed', original).premise_hash(policy='p') != NodeTaskDialogue('fixed', changed).premise_hash(policy='p')


def test_current_diagnostics_override_stale_payload_and_remain_in_current_tail():
    ctx = context()
    payload = {'phase': 'node_assess', 'structural_diagnostics': [{'code': 'stale'}]}
    ctx.node_structural_diagnostics = [{'code': 'self_relation', 'edge_id': 'e1'}]
    before = build_node_packet(ctx, payload)
    assert before['structural_diagnostics'] == ctx.node_structural_diagnostics
    ctx.node_structural_diagnostics = []
    after = build_node_packet(ctx, payload)
    assert after['structural_diagnostics'] == []
    old, new = NodeTaskDialogue('stable', before), NodeTaskDialogue('stable', after)
    assert old.messages()[:-1] == new.messages()[:-1]
    assert old.messages()[-1] != new.messages()[-1]


def test_explicit_relation_removal_keeps_complete_before_assertion():
    ctx = context()
    ctx.active_draft = {'relation_removals': [{'edge_id': 'e1', 'reason': 'Controlled test removal.'}]}
    ctx.candidate = ctx.candidate.model_copy(update={'graph': ctx.candidate.graph.model_copy(update={
        'edges': tuple(e for e in ctx.candidate.graph.edges if e.id != 'e1')})})
    packet = build_node_packet(ctx, {'phase': 'node_assess'})
    assert packet['relation_removals'] == ctx.active_draft['relation_removals']
    original = next(e for e in packet['before_relations'] if e['id'] == 'e1')
    assert original['properties']['description'] == 'Representation A'
    assert original['from_node_id'] == 'N1' and original['to_node_id'] == 'a'
    assert 'e1' not in {e['id'] for e in packet['candidate']['relations']}
