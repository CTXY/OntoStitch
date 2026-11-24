"""Transport diagnostics must not instruct agents to enumerate unrelated context."""
import pytest
from graph_merge_fixtures import catalog, concept, state

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.consolidation.context import compact_task_context
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask, build_task_context
from cortex.schema import load_root_schema


@pytest.mark.parametrize('for_review', [True, False])
def test_deferred_context_is_optional_and_required_knowledge_is_unchanged(for_review):
    ctx = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    candidate = ctx.union_state()
    node_id = ctx.input_node_map['A']
    task = LocalTask('local', 'node_assess' if for_review else 'consolidate', {node_id})
    payload = build_task_context(ctx, candidate, task, before=candidate)
    original = next(n for n in payload['candidate_nodes'] if n['id'] == node_id)
    old_properties = dict(original['properties'])
    query = {'mode': 'relations', 'ids': [node_id], 'offset': 0, 'limit': 40}
    payload['task_scope'].update({
        'complete': False, 'required_units_complete': True,
        'original_characters': 155464, 'soft_character_budget': 24000,
        'budget_exceeded_by_required_units': True,
        'deferred_context': [{'view': 'candidate', 'unloaded_nodes': 17, 'unloaded_relations': 19,
                              'next_query': query, 'note': 'Inspect adjacent relation pages, then request complete endpoints.'}]})
    result = compact_task_context(payload, ctx, candidate, {node_id}, [], for_review=for_review)
    scope = result['task_scope']
    assert scope['required_units_complete'] is True and scope['complete'] is False
    assert not {'original_characters', 'soft_character_budget', 'budget_exceeded_by_required_units'} & scope.keys()
    deferred = scope['deferred_context'][0]
    assert deferred['unloaded_nodes'] == 17 and deferred['unloaded_relations'] == 19
    assert deferred['next_query'] == query
    assert 'only when a specific decision lacks a premise' in deferred['note']
    assert 'not a reading checklist' in deferred['note']
    assert next(n for n in result['candidate_nodes'] if n['id'] == node_id)['properties'] == old_properties
    assert result['nodes']  # Complete source members remain available for comparison.


@pytest.mark.parametrize('current_members,expected', [(['A'], 'A'), (['A', 'B'], None), ([], None)])
def test_current_owner_only_when_one_source_group_member_is_present(current_members, expected):
    source = state([*catalog(), concept('A', 'a'), concept('B', 'a')])
    ctx = IntegrationContext(state(), source, load_root_schema())
    all_nodes = ctx.union_state()
    members = [ctx.input_node_map[k] for k in ('A', 'B')]
    kept = {ctx.input_node_map[k] for k in current_members}
    candidate = all_nodes.model_copy(update={'graph': all_nodes.graph.model_copy(update={
        'nodes': tuple(n for n in all_nodes.graph.nodes if n.id not in members or n.id in kept)})})
    group = {'members': members, 'meaning': 'Same referent', 'reason': 'Confirmed matching'}
    task = LocalTask('owners', 'consolidate', set(members), groups=[group])
    payload = build_task_context(ctx, candidate, task, before=all_nodes)
    payload = compact_task_context(payload, ctx, candidate, set(members), [group], for_review=False)
    result = payload['matched_groups'][0]
    assert result.get('current_owner') == (ctx.input_node_map[expected] if expected else None)
    assert result['members'] == members
    assert 'current_owner' not in group  # Transport annotation cannot mutate controller identity groups.


def test_identity_predicate_filter_requires_nonempty_list_and_current_owner_is_explicit():
    from graph_merge_fixtures import edge
    from cortex.capabilities.graph_merge.identity.tools import build_tools

    source = state([*catalog(), concept('A', 'a'), concept('B', 'b')],
                   [edge('ab', 'A', 'B', 'has_feature')])
    ctx = IntegrationContext(state(), source, load_root_schema())
    ctx.candidate = ctx.union_state()
    a = ctx.input_node_map['A']
    group = {'members': [a], 'meaning': 'Same referent', 'reason': 'Confirmed matching'}
    task = LocalTask('content', 'consolidate', {a}, groups=[group])
    payload = compact_task_context(build_task_context(ctx, ctx.candidate, task, before=ctx.candidate),
                                   ctx, ctx.candidate, {a}, [group], for_review=False)
    assert 'provisional_identity_projection' not in payload
    assert payload['matched_groups'][0]['current_owner'] == a
    assert 'current_owner' not in group
    # Graph inspection belongs to Identity. Node roles receive a complete group
    # and only their evidence tool; do not exercise graph tools via a Node call.
    ctx.payload('identity')
    inspect = next(tool for tool in build_tools(ctx) if tool.name == 'inspect_graph')
    rejected = inspect.invoke({'mode': 'relations', 'ids': [a], 'edge_types': []})
    assert rejected == {'error': 'edge_types requires mode=relations and a nonempty list'}
    response = inspect.invoke({'mode': 'relations', 'ids': [a]})
    assert 'error' not in response
    assert response['next_offset'] is None and response['total'] == 1
    assert [row['id'] for row in response['items']] == [ctx.input_edge_map['ab']]
    filtered = inspect.invoke({'mode': 'relations', 'ids': [a], 'edge_types': ['has_feature']})
    assert filtered['items'] == response['items']
    assert filtered['next_offset'] is None and filtered['total'] == response['total']
