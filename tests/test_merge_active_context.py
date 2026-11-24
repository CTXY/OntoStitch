"""Active requests preserve task facts without accumulating replaced drafts."""
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from cortex.capabilities.graph_merge.llm.packets import (
    TaskDialogue,
    compact_node_packet,
)


def action(name, args, ident):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': ident}])


def test_projection_is_idempotent_and_preserves_explicit_view_difference():
    shared = {'id': 'N', 'properties': {'definition': 'Original meaning'}}
    packet = {'nodes': [shared], 'candidate_nodes': [shared], 'relations': [], 'candidate_relations': []}
    projected = compact_node_packet(packet)
    assert compact_node_packet(projected) == projected
    changed = {**shared, 'properties': {'definition': 'Changed meaning'}}
    assert compact_node_packet({**packet, 'candidate_nodes': [changed]})['nodes'] == [shared]


def test_many_replacements_keep_one_draft_all_semantic_issues_and_unique_reads():
    payload = {'draft': {'explanation': 'old checkpoint draft'},
               'semantic_issues': [{'problem': 'Preserve the boundary at 1.5'}]}
    dialogue = TaskDialogue('Fixed contract', payload)
    answer = {'view': 'before', 'revision': 7, 'items': [{'id': 'N', 'definition': 'Necessary premise'}]}
    dialogue.exchange(action('inspect_graph', {'ids': ['N'], 'view': 'before'}, 'read'), [('read', answer)], result_name='submit_patch')
    sizes = []
    for number in range(30):
        draft = {'explanation': f'version-{number:02}', 'content': 'complete semantic content ' * 800}
        call = action('submit_patch', draft, f'patch-{number}')
        dialogue.exchange(call, [(f'patch-{number}', {'status': 'rejected_unapplied', 'error': 'One exact field needs correction'})], result_name='submit_patch')
        messages = dialogue.messages()
        sizes.append(len(json.dumps([m.model_dump(mode='json') for m in messages])))
        body = json.loads(messages[1].content)
        assert body['semantic_issues'] == payload['semantic_issues']
        assert body['draft'] == draft
        assert not any(c['name'] == 'submit_patch' for m in messages if isinstance(m, AIMessage) for c in m.tool_calls)
        assert any(isinstance(m, ToolMessage) and 'Necessary premise' in m.content for m in messages)
    assert max(sizes) - min(sizes) < 20
    assert payload['draft']['explanation'] == 'old checkpoint draft'


def test_draft_echo_dedup_keeps_parallel_graph_premise_and_matching_tool_ids():
    dialogue = TaskDialogue('Contract', {})
    draft = {'explanation': 'latest complete draft'}
    dialogue.exchange(action('submit_patch', draft, 'p'), [('p', {'status': 'rejected_unapplied'})], result_name='submit_patch')
    parallel = AIMessage(content='', tool_calls=[
        {'name': 'inspect_draft', 'args': {}, 'id': 'd'},
        {'name': 'inspect_graph', 'args': {'ids': ['N']}, 'id': 'g'}])
    dialogue.exchange(parallel, [('d', {'status': 'draft', 'draft': draft}),
        ('g', {'view': 'before', 'items': [{'id': 'N', 'name': 'Keep this complete value'}]})], result_name='submit_patch')
    newer = {'explanation': 'next draft'}
    dialogue.exchange(action('submit_patch', newer, 'next'), [('next', {'status': 'rejected_unapplied'})], result_name='submit_patch')
    messages = dialogue.messages()
    assert any(isinstance(m, ToolMessage) and m.tool_call_id == 'g' for m in messages)
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == 'd' for m in messages)
    for i, message in enumerate(messages):
        if isinstance(message, AIMessage):
            ids = {c['id'] for c in message.tool_calls}
            assert {m.tool_call_id for m in messages[i + 1:i + 1 + len(ids)] if isinstance(m, ToolMessage)} == ids


def test_different_read_views_and_versions_are_not_deduplicated():
    dialogue = TaskDialogue('Contract', {})
    for i, (view, meaning) in enumerate([('before', 'Old'), ('candidate', 'New'), ('candidate', 'Newer')]):
        call = action('inspect_graph', {'ids': ['N'], 'view': view}, str(i))
        result = {'view': view, 'revision': i, 'items': [{'id': 'N', 'definition': meaning}]}
        dialogue.exchange(call, [(str(i), result)], result_name='submit_patch')
    assert len([m for m in dialogue.messages() if isinstance(m, ToolMessage)]) == 3


def test_plain_json_failures_do_not_echo_raw_narration_or_accumulate_schemas():
    dialogue = TaskDialogue('Contract', {'semantic_issues': [{'problem': 'Still unresolved'}]})
    for i in range(10):
        dialogue.plain_failure(AIMessage(content='Unstructured narration ' * 1000), {'error': f'error-{i}'})
        dialogue.use_fallback({'type': 'object'})
    messages = dialogue.messages()
    assert len(messages) == 3
    assert sum('plain JSON' in m.content for m in messages) == 1
    assert 'Unstructured narration' not in str(messages)
    assert json.loads(messages[1].content)['semantic_issues']


def test_five_real_thrombosis_patches_rebuild_instead_of_accumulating():
    import gzip
    from pathlib import Path

    from cortex.capabilities.graph_merge.llm.budget import check_request
    folder = Path(__file__).parent / 'fixtures/thrombosis_contract_failure'
    with gzip.open(folder / 'initial_packet.json.gz', 'rt') as stream:
        packet = json.load(stream)
    with gzip.open(folder / 'failed_patches.json.gz', 'rt') as stream:
        failures = json.load(stream)
    dialogue = TaskDialogue('Same fixed task contract', packet)
    sizes = []
    for number, failure in enumerate(failures):
        draft = failure['patch']
        call = action('submit_patch', draft, f'actual-{number}')
        dialogue.exchange(call, [(f'actual-{number}', {
            'status': 'rejected_unapplied', 'error': 'The real source disposition contract was not satisfied'})],
            result_name='submit_patch')
        dialogue.exchange(action('inspect_draft', {}, f'echo-{number}'),
                          [(f'echo-{number}', {'status': 'draft', 'draft': draft})], result_name='submit_patch')
        messages = dialogue.messages()
        sizes.append(check_request(messages, [], max_characters=400000))
        active = [call for m in messages if isinstance(m, AIMessage) for call in m.tool_calls]
        assert not any(call['name'] == 'submit_patch' for call in active)
        assert json.loads(messages[1].content)['draft'] == draft
        assert not any(call['name'] == 'inspect_draft' for call in active)
        assert json.loads(messages[1].content)['evidence'] == packet['evidence']
    assert len(failures) == 5
    assert max(sizes) < 250000
    assert max(sizes) - min(sizes) < 3000
    projected = compact_node_packet(packet)
    assert len(projected['source_equals_candidate']['nodes']) == 48
    assert len(projected['source_equals_candidate']['relations']) == 66


def test_malformed_tool_context_keeps_ids_without_replaying_large_raw_output():
    dialogue = TaskDialogue('Contract', {})
    response = AIMessage(content='Unstructured narration ' * 10000, invalid_tool_calls=[{
        'id': 'broken', 'name': 'submit_patch', 'args': 'Malformed JSON ' * 10000, 'error': 'Invalid JSON'}])
    dialogue.exchange(response, [('broken', 'Malformed tool JSON; no proposal was applied.')], result_name='submit_patch')
    messages = dialogue.messages()
    assert 'Malformed tool JSON' in messages[1].content
    assert not any(isinstance(m, ToolMessage) for m in messages)
    assert len(str(messages)) < 2000
    assert len(response.content) > 100000


@pytest.mark.parametrize('next_kind', ['malformed_tool', 'unknown_tool', 'plain_narration'])
def test_later_format_error_never_loses_latest_complete_draft_or_its_validation(next_kind):
    draft = {'explanation': 'The latest complete draft', 'integrate_groups': []}
    feedback = {'status': 'rejected_unapplied', 'error': 'Extra unit S6'}
    dialogue = TaskDialogue('Contract', {'semantic_issues': [{'problem': 'Preserve knowledge'}]})
    dialogue.exchange(action('submit_patch', draft, 'p'), [('p', feedback)], result_name='submit_patch')
    if next_kind == 'plain_narration':
        dialogue.plain_failure(AIMessage(content='Cannot parse this'), {'error': 'Invalid JSON'})
    elif next_kind == 'unknown_tool':
        dialogue.exchange(action('nonexistent', {}, 'u'), [('u', {'error': 'Unknown tool'})], result_name='submit_patch')
    else:
        response = AIMessage(content='', invalid_tool_calls=[{'id': 'm', 'name': 'submit_patch', 'args': '{', 'error': 'Invalid JSON'}])
        dialogue.exchange(response, [('m', 'Malformed tool JSON')], result_name='submit_patch')
    dialogue.use_fallback({'type': 'object'})
    messages = dialogue.messages()
    packet = json.loads(messages[1].content)
    assert packet['draft'] == draft
    assert packet['issues'] == [feedback]
    assert packet['semantic_issues']


@pytest.mark.parametrize('name,ident', [(None, None), ('submit_patch', None), (None, 'broken')])
def test_malformed_call_without_name_or_id_uses_plain_feedback(name, ident):
    from langchain_openai.chat_models.base import _convert_message_to_dict
    dialogue = TaskDialogue('Contract', {})
    draft = {'explanation': 'Keep this latest complete draft'}
    dialogue.exchange(action('submit_patch', draft, 'p'), [('p', {'status': 'rejected_unapplied'})], result_name='submit_patch')
    malformed = AIMessage(content='', invalid_tool_calls=[{
        'name': name, 'id': ident, 'args': '{', 'error': 'Invalid JSON'}])
    dialogue.exchange(malformed, [(ident, 'Malformed JSON')], result_name='submit_patch')
    messages = dialogue.messages()
    assert json.loads(messages[1].content)['draft'] == draft
    for message in messages:
        wire = _convert_message_to_dict(message)
        assert not wire.get('tool_calls')
    assert 'lacks a valid name or call ID' in messages[-1].content
