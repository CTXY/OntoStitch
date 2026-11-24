"""Real multi-submit failures must allow a separate feedback/repair round."""
import gzip
import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai.chat_models.base import _convert_message_to_dict
from test_graph_integration import state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.packets import TaskDialogue
from cortex.capabilities.graph_merge.llm.task_agent import (
    IncrementalIntegrationAgent,
    OutputProtocolRecoveryExhausted,
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


def test_real_toxicology_reply_gets_protocol_then_schema_feedback_before_repair(real, tmp_path):
    from cortex.infrastructure.artifacts.graph_merge_trace import (
        FileSystemGraphMergeTraceSink,
    )

    case = real['toxicology_multi']
    assert [bool(c['args']) for c in case['calls']] == [False, True, False]
    draft = next(c['args'] for c in case['calls'] if c['args'])
    # The substantive proposal also has a real extra field, previously hidden
    # behind the protocol failure. It must receive ordinary schema feedback.
    repaired = deepcopy(draft)
    repaired.pop('explanation_alt_note')
    ctx, saved, errors = context()
    prior = deepcopy(case['payload']['draft'])
    ctx.active_draft = prior
    model = Chat([action(case['calls']), submission(draft), AIMessage(content=json.dumps(repaired))])
    trace = tmp_path / 'agents.jsonl'
    result = IncrementalIntegrationAgent(model, max_calls=None,
        trace_sink=FileSystemGraphMergeTraceSink(trace)).invoke('reconcile', ctx, case['payload'])
    assert len(model.requests) == 3
    assert saved == [draft, repaired] and len(errors) == 1
    assert result.explanation == draft['explanation']
    assert draft_bodies(model.requests[1]) == [prior]
    assert 'output_protocol' in model.requests[1][-1].content
    assert draft_bodies(model.requests[2]) == [draft]
    assert 'explanation_alt_note' in str(model.requests[2])
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    rejected = [e for e in events if e.get('event_type') == 'tool_result'][:3]
    assert [e['arguments'] for e in rejected] == [c['args'] for c in case['calls']]
    assert all(e['result']['code'] == 'output_protocol' for e in rejected)


def test_repeated_empty_placeholders_with_changed_full_proposal_are_not_no_progress():
    def multi(number):
        return action([{'name': 'submit_patch', 'args': {}, 'id': f'empty-{number}-a'},
                       {'name': 'submit_patch', 'args': {'explanation': f'proposal-{number}'}, 'id': f'full-{number}'},
                       {'name': 'submit_patch', 'args': {}, 'id': f'empty-{number}-b'}])
    ctx, saved, errors = context()
    prior = {'explanation': 'checkpoint draft'}
    ctx.active_draft = prior
    model = Chat([multi(1), multi(2), submission({'explanation': 'valid repair'})])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, {'draft': prior})
    assert len(model.requests) == 3 and saved == [{'explanation': 'valid repair'}] and not errors
    for messages in model.requests[1:]:
        assert draft_bodies(messages) == [prior]


def test_identical_multi_submit_stops_only_after_a_separate_model_round(real):
    case = real['toxicology_multi']
    ctx, saved, errors = context()
    prior = deepcopy(case['payload']['draft'])
    ctx.active_draft = prior
    repeated = deepcopy(case['calls'])
    for c in repeated:
        c['id'] += '-again'
    model = Chat([action(case['calls']), action(repeated), action(repeated)])
    with pytest.raises(OutputProtocolRecoveryExhausted, match='three separate model responses'):
        IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, case['payload'])
    assert len(model.requests) == 3 and not saved and not errors
    assert ctx.active_draft == prior


def test_protocol_budget_counts_rounds_without_call_ids_order_or_placeholder_count():
    first = [{'name': 'submit_patch', 'args': {}, 'id': 'empty'},
             {'name': 'submit_patch', 'args': {'explanation': 'same proposal'}, 'id': 'full'}]
    second = [deepcopy(first[1]), deepcopy(first[0]), deepcopy(first[0])]
    for i, call in enumerate(second):
        call['id'] = f'new-{i}'
    ctx, saved, errors = context()
    model = Chat([action(first), action(second), action(first)])
    with pytest.raises(OutputProtocolRecoveryExhausted, match='three separate model responses'):
        IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, {})
    assert len(model.requests) == 3 and not saved and not errors


@pytest.mark.parametrize('phase,name,output', [
    ('identity', 'IdentityDecision', {'groups': [], 'distinctions': []}),
    ('graph_assess', 'Review', {'verdict': 'accept'}),
])
def test_protocol_repair_also_applies_to_identity_and_assessment(phase, name, output):
    calls = [{'name': name, 'args': output, 'id': 'one'},
             {'name': name, 'args': output, 'id': 'two'}]
    ctx, saved, errors = context()
    model = Chat([action(calls), action([{'name': name, 'args': output, 'id': 'single'}])])
    IncrementalIntegrationAgent(model, max_calls=None).invoke(phase, ctx, {})
    assert len(model.requests) == 2 and not saved and not errors
    assert 'output_protocol' in model.requests[1][-1].content


def test_mixed_read_and_submission_is_rejected_atomically_before_read_or_draft_write(monkeypatch):
    from langchain_core.tools import tool

    from cortex.capabilities.graph_merge.llm import task_agent
    reads = []

    @tool
    def inspect_graph(ids: list[str]) -> dict:
        """Read an exact graph premise."""
        reads.append(ids)
        return {'items': []}

    monkeypatch.setattr(task_agent, 'build_read_tools', lambda *args, **kwargs: {'inspect_graph': inspect_graph})
    prior = {'explanation': 'original'}
    mixed = action([{'name': 'inspect_graph', 'args': {'ids': ['N']}, 'id': 'read'},
                    {'name': 'submit_patch', 'args': {'explanation': 'unapplied'}, 'id': 'patch'}])
    ctx, saved, errors = context()
    model = Chat([mixed, submission({'explanation': 'valid'})])
    IncrementalIntegrationAgent(model).invoke('reconcile', ctx, {'draft': prior})
    assert not reads and not errors and saved == [{'explanation': 'valid'}]
    assert draft_bodies(model.requests[1]) == [prior]


def test_real_draft_projection_is_preserved_without_becoming_the_complete_draft(real):
    prior = real['echo']['payload']['draft']
    dialogue = TaskDialogue('Contract', {'draft': prior})
    projection = {'status': 'draft', 'draft': {'explanation': prior['explanation']},
                  'projection': {'sections': ['explanation']}}
    dialogue.exchange(action([{'name': 'inspect_draft', 'args': {'sections': ['explanation']}, 'id': 'part'}]),
                      [('part', projection)], result_name='submit_patch')
    assert draft_bodies(dialogue.messages()) == [prior]
    assert json.loads(dialogue.messages()[-1].content) == projection


def test_duplicate_malformed_calls_need_a_separate_model_round_to_stop():
    malformed = AIMessage(content='', invalid_tool_calls=[
        {'name': 'submit_patch', 'args': '{', 'id': 'bad-a', 'error': 'Invalid JSON'},
        {'name': 'submit_patch', 'args': '{', 'id': 'bad-b', 'error': 'Invalid JSON'}])
    ctx, saved, errors = context()
    model = Chat([malformed, submission({'explanation': 'valid'})])
    IncrementalIntegrationAgent(model, max_calls=None).invoke('reconcile', ctx, {})
    assert len(model.requests) == 2 and saved == [{'explanation': 'valid'}] and not errors
