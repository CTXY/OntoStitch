"""Real multi-submit failures must allow a separate feedback/repair round."""
import gzip
import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai.chat_models.base import _convert_message_to_dict
from graph_merge_fixtures import state
from graph_merge_chat import Chat

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.llm.packets import TaskDialogue
from cortex.capabilities.graph_merge.llm.agent import (
    IncrementalIntegrationAgent,
)
from cortex.schema import load_root_schema


@pytest.fixture
def real():
    path = Path(__file__).parent / 'fixtures/merge_output_protocol_failure/real_requests.json.gz'
    with gzip.open(path, 'rt') as stream:
        return json.load(stream)


def action(calls):
    return AIMessage(content='', tool_calls=deepcopy(calls))


def submission(draft, ident='single'):
    return action([{'name': 'submit_patch', 'args': draft, 'id': ident}])


def draft_bodies(messages):
    """All non-projected draft bodies that would actually go to the provider."""
    result = []
    for message in messages:
        if isinstance(message, AIMessage):
            result.extend(c['args'] for c in message.tool_calls if c['name'] == 'submit_patch')
        if isinstance(message.content, str):
            try:
                value = json.loads(message.content)
            except ValueError:
                continue
            if isinstance(value, dict) and 'draft' in value and not value.get('projection'):
                result.append(value['draft'])
    return result


def context():
    ctx = IntegrationContext(state(), state(), load_root_schema())
    ctx.candidate = ctx.union_state()
    saved, errors = [], []
    ctx.record_task_draft = saved.append
    ctx.record_task_error = errors.append
    ctx.compile_task_patch = lambda patch: object()
    return ctx, saved, errors


def test_real_checkpoint_draft_echo_has_one_body_even_before_first_repair(real):
    case = real['echo']
    original = deepcopy(case['payload'])
    dialogue = TaskDialogue('Contract', case['payload'])
    read = action([{'name': 'inspect_draft', 'args': case['call'], 'id': 'echo'}])
    dialogue.exchange(read, [('echo', case['result'])], result_name='submit_patch')
    assert draft_bodies(dialogue.messages()) == [case['payload']['draft']]
    assert case['payload'] == original


def test_initial_draft_echo_in_parallel_keeps_graph_read_and_tool_pairing(real):
    case = real['echo']
    dialogue = TaskDialogue('Contract', case['payload'])
    read = action([{'name': 'inspect_draft', 'args': {}, 'id': 'echo'},
                   {'name': 'inspect_graph', 'args': {'ids': ['N']}, 'id': 'graph'}])
    premise = {'view': 'before', 'revision': 1, 'items': [{'id': 'N', 'name': 'Exact premise'}]}
    dialogue.exchange(read, [('echo', case['result']), ('graph', premise)], result_name='submit_patch')
    messages = dialogue.messages()
    assert draft_bodies(messages) == [case['payload']['draft']]
    assert [m.tool_call_id for m in messages if isinstance(m, ToolMessage)] == ['graph']
    assert json.loads(messages[-1].content) == premise
    assert messages[-2].tool_calls[0]['id'] == messages[-1].tool_call_id


@pytest.mark.parametrize('name', ['thrombosis_multi', 'toxicology_multi'])
def test_real_multi_submit_preserves_previous_draft_without_echoing_rejected_bodies(real, name):
    case = real[name]
    prior = real['echo']['payload']['draft']
    dialogue = TaskDialogue('Contract', {'draft': prior, 'semantic_issues': [{'problem': 'Unresolved'}]})
    response = action(case['calls'])
    original = deepcopy(response)
    errors = [(c['id'], {'status': 'rejected_unapplied', 'error': 'Submit one result alone'})
              for c in response.tool_calls]
    dialogue.exchange(response, errors, result_name='submit_patch')
    messages = dialogue.messages()
    assert draft_bodies(messages) == [prior]
    assert dialogue.latest_draft == prior
    assert json.loads(messages[1].content)['semantic_issues']
    assert 'output_protocol' in messages[-1].content
    assert all(not _convert_message_to_dict(m).get('tool_calls') for m in messages)
    assert response == original  # Full provider output remains available for the audit.


def test_real_draft_projection_is_preserved_without_becoming_the_complete_draft(real):
    prior = real['echo']['payload']['draft']
    dialogue = TaskDialogue('Contract', {'draft': prior})
    projection = {'status': 'draft', 'draft': {'explanation': prior['explanation']},
                  'projection': {'sections': ['explanation']}}
    dialogue.exchange(action([{'name': 'inspect_draft', 'args': {'sections': ['explanation']}, 'id': 'part'}]),
                      [('part', projection)], result_name='submit_patch')
    assert draft_bodies(dialogue.messages()) == [prior]
    assert json.loads(dialogue.messages()[-1].content) == projection


def test_identity_rejects_multiple_results_before_accepting_one_complete_decision():
    calls = [{'name': 'IdentityDecision', 'args': {'groups': [], 'distinctions': []}, 'id': ident}
             for ident in ('one', 'two')]
    ctx, saved, errors = context()
    model = Chat([action(calls), action([calls[0]])])
    result = IncrementalIntegrationAgent(model, max_calls=None).invoke('identity', ctx, {})
    assert not result.groups and not result.distinctions
    assert len(model.requests) == 2 and not saved and not errors
    assert 'output_protocol' in model.requests[1][-1].content
