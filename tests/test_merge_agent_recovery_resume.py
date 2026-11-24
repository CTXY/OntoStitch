"""Canonical recovery preserves failure budgets and rejects corrupt checkpoints."""
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from graph_merge_chat import ScriptedModel
from cortex.capabilities.graph_merge.llm.recovery import OutputProtocolRecoveryExhausted, RecoveryTracker, TaskNoProgress
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from test_reconciliation_agent_runtime import fixture, call, edit
from test_reconciliation_preview_checkpoint import restart


ACCEPT = AIMessage(content=json.dumps({'explanation': 'The local result preserves supported meaning and evidence.', 'feedback': []}))


def interrupted(tmp_path, replies, run='recovery'):
    chat = ScriptedModel([*replies, RuntimeError('process interrupted')])
    engine, task = fixture(tmp_path, chat)
    before = engine.current
    with pytest.raises(RuntimeError, match='process interrupted'):
        engine._run_task(task, run)
    saved = WorkspaceStore(engine.workspace_path).load(run)
    assert engine.current == before and not engine.history
    return saved


@pytest.mark.parametrize('corruption', ['missing_tracker', 'null_tracker', 'empty', 'missing_requests', 'runtime_hash', 'tracker_hash'])
def test_corrupt_recovery_envelope_is_rejected_before_model_call(tmp_path, corruption):
    saved = interrupted(tmp_path, [call('edit_draft', {'unexpected': True})])
    records = saved['task_state']['agent_recovery']
    key = next(key for key in records if key.startswith('simple:'))
    record = records[key]
    if corruption == 'empty': records[key] = {}
    elif corruption == 'missing_tracker': record.pop('tracker')
    elif corruption == 'null_tracker': record['tracker'] = None
    elif corruption == 'missing_requests': record.pop('requests')
    else: record[corruption] = 'tampered'
    chat = ScriptedModel([])
    engine, task = restart(tmp_path, saved, chat)
    before = engine.current
    with pytest.raises(ValueError, match='Incomplete or mismatched'):
        engine._run_task(task, 'recovery')
    assert not chat.requests and engine.current == before and not engine.history


@pytest.mark.parametrize('kind', ['schema', 'compiler'])
def test_content_failure_budget_survives_restart_and_terminal_retry_calls_no_model(tmp_path, kind):
    bad = {'unexpected': True} if kind == 'schema' else edit(target='missing-endpoint')
    saved = interrupted(tmp_path, [call('edit_draft', bad)])
    chat = ScriptedModel([call('edit_draft', bad)])
    engine, task = restart(tmp_path, saved, chat)
    with pytest.raises(TaskNoProgress): engine._run_task(task, 'recovery')
    assert len(chat.requests) == 1 and not engine.history
    saved = WorkspaceStore(engine.workspace_path).load('recovery')
    empty = ScriptedModel([])
    engine, task = restart(tmp_path, saved, empty)
    with pytest.raises(TaskNoProgress): engine._run_task(task, 'recovery')
    assert not empty.requests and not engine.history


def test_assessment_result_only_recovery_survives_process_restart(tmp_path):
    bad = AIMessage(content='Narration before JSON')
    saved = interrupted(tmp_path, [call('edit_draft', {}), bad, bad])
    chat = ScriptedModel([ACCEPT])
    engine, task = restart(tmp_path, saved, chat)
    engine._run_task(task, 'recovery')
    assert len(chat.requests) == 1 and chat.requests[0][1] == []
    assert 'Required JSON schema' in chat.requests[0][0][-1].content
    assert len(engine.history) == 1


def test_protocol_budget_survives_restart_and_exhaustion_is_terminal(tmp_path):
    bad = AIMessage(content='Narration before JSON')
    saved = interrupted(tmp_path, [call('edit_draft', {}), bad, bad])
    chat = ScriptedModel([bad])
    engine, task = restart(tmp_path, saved, chat)
    with pytest.raises(OutputProtocolRecoveryExhausted): engine._run_task(task, 'recovery')
    assert len(chat.requests) == 1 and chat.requests[0][1] == []
    saved = WorkspaceStore(engine.workspace_path).load('recovery')
    chat = ScriptedModel([])
    engine, task = restart(tmp_path, saved, chat)
    with pytest.raises(OutputProtocolRecoveryExhausted): engine._run_task(task, 'recovery')
    assert not chat.requests and not engine.history


def test_new_visible_facts_allow_retry_without_erasing_old_failure_buckets():
    tracker = RecoveryTracker()
    tracker.observe('draft_compile', {'change': 'same'}, {'error': 'same'}, 'facts-before')
    with pytest.raises(TaskNoProgress): tracker.observe('draft_compile', {'change': 'same'}, {'error': 'same'}, 'facts-before')
    saved = deepcopy(tracker.data)
    restored = RecoveryTracker(saved)
    with pytest.raises(TaskNoProgress): restored.ensure_active(facts_hash='facts-before')
    restored.ensure_active(facts_hash='facts-after-explicit-read')
    assert restored.data['failures'] == saved['failures']
    restored.observe('draft_compile', {'change': 'same'}, {'error': 'same'}, 'facts-after-explicit-read')
    with pytest.raises(TaskNoProgress): restored.observe('draft_compile', {'change': 'same'}, {'error': 'same'}, 'facts-before')
    assert saved['blocked'] is not None  # Restoring never mutates the saved JSON.


def test_runtime_bookkeeping_and_failed_or_duplicate_reads_do_not_create_facts():
    from test_reconciliation_context import runtime
    rt = runtime()
    before = rt.premise_hash()
    rt.set_feedback({'protocol_error': {'message': 'different narration'}, 'transport_metadata': 'new attempt'})
    assert rt.premise_hash() == before
    with pytest.raises(ValueError): rt.read_evidence(ids=['not-authorized'])
    assert rt.premise_hash() == before
    rt.read_evidence(ids=['ev-A'])
    after = rt.premise_hash()
    assert after != before
    rt.read_evidence(ids=['ev-A'])
    assert rt.premise_hash() == after
    with pytest.raises(ValueError): rt.read_evidence(ids=['ev-A', 'not-authorized'])
    assert rt.premise_hash() == after
    tracker = RecoveryTracker()
    tracker.observe('draft_compile', {}, {'error': 'same'}, before)
    with pytest.raises(TaskNoProgress): tracker.observe('draft_compile', {}, {'error': 'same'}, before)
    restored = RecoveryTracker(tracker.data)
    with pytest.raises(TaskNoProgress): restored.ensure_active(facts_hash=before)
    restored.ensure_active(facts_hash=after)
