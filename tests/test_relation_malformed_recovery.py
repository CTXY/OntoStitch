"""Canonical malformed actions are rejected without legacy Patch fallbacks."""

import pytest
from langchain_core.messages import AIMessage

from graph_merge_chat import ScriptedModel
from cortex.capabilities.graph_merge.llm.recovery import OutputProtocolRecoveryExhausted
from test_reconciliation_agent_runtime import fixture, call
from test_merge_agent_recovery_resume import ACCEPT


def malformed(ident='broken'):
    return AIMessage(content='', invalid_tool_calls=[{'name': 'edit_draft', 'args': '{"put_changes":',
        'id': ident, 'error': 'incomplete JSON', 'type': 'invalid_tool_call'}])


@pytest.mark.parametrize('bad', [malformed(), AIMessage(content='[{"put_changes": []}]'),
                               AIMessage(content='{"explanation": "Removed plain Patch fallback"}')])
def test_malformed_action_repairs_to_current_edit_draft_without_saving_invalid_raw(tmp_path, bad):
    def valid(model, messages):
        rt = model.engine.session.inputs.reconciliation_runtime
        assert rt.draft.version == 0 and not rt.draft.changes
        assert rt.feedback['protocol_error']
        assert len(messages) == 3
        return call('edit_draft', {})
    chat = ScriptedModel([bad, valid, ACCEPT])
    engine, task = fixture(tmp_path, chat)
    engine._run_task(task, 'malformed')
    assert len(chat.requests) == 3 and len(engine.session.history) == 1


@pytest.mark.parametrize('kind', ['mixed_reads', 'unknown_tool', 'multiple_actions'])
def test_invalid_action_batch_is_atomic_before_any_read_or_draft_side_effect(tmp_path, kind):
    bad = malformed()
    valid = call('edit_draft', {})
    if kind == 'mixed_reads':
        response = AIMessage(content='', invalid_tool_calls=bad.invalid_tool_calls,
            tool_calls=[{'name': 'read_predicates', 'id': 'read', 'args': {'from_node_type': 'concept', 'to_node_type': 'concept'}}])
    elif kind == 'unknown_tool':
        response = call('submit_patch', {'explanation': 'Removed external protocol'})
    else:
        response = AIMessage(content='', tool_calls=[*valid.tool_calls, {**valid.tool_calls[0], 'id': 'another'}])
    def after(model, messages):
        rt = model.engine.session.inputs.reconciliation_runtime
        assert not rt._journal and rt.draft.version == 0 and not rt.draft.changes
        assert rt.feedback['protocol_error']
        return call('edit_draft', {})
    chat = ScriptedModel([response, after, ACCEPT])
    engine, task = fixture(tmp_path, chat)
    engine._run_task(task, 'atomic-malformed')
    assert len(engine.session.history) == 1


def test_malformed_round_budget_ignores_call_ids_and_invalid_placeholder_count(tmp_path):
    first = malformed('one')
    second = AIMessage(content='', invalid_tool_calls=[
        *malformed('two').invalid_tool_calls, *malformed('three').invalid_tool_calls])
    chat = ScriptedModel([first, second, malformed('four')])
    engine, task = fixture(tmp_path, chat)
    before = engine.session.current
    with pytest.raises(OutputProtocolRecoveryExhausted): engine._run_task(task, 'budget')
    assert len(chat.requests) == 3 and engine.session.current == before and not engine.session.history
