"""Current relation transactions resume acceptance without replaying models."""
from copy import deepcopy

import pytest

from graph_merge_chat import ScriptedModel
from graph_merge_runtime_fixtures import setup_engine as setup_engine
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from test_reconciliation_agent_runtime import fixture, call, edit
from test_reconciliation_preview_checkpoint import restart
from test_merge_agent_recovery_resume import ACCEPT, interrupted


def test_ready_acceptance_after_failed_storage_resumes_without_any_model_call(tmp_path):
    chat = ScriptedModel([call('edit_draft', edit()), ACCEPT])
    engine, task = fixture(tmp_path, chat)
    before = engine.session.current
    persist = engine._persist
    def fail_commit(run_id):
        if engine.session.task_state.stage == 'committed':
            raise OSError('Storage commit unavailable')
        persist(run_id)
    engine._persist = fail_commit
    with pytest.raises(OSError, match='Storage commit'):
        engine._run_task(task, 'commit')
    assert engine.session.current == before and not engine.session.history
    saved = WorkspaceStore(engine.workspace_path).load('commit')
    assert saved['task_state']['stage'] == 'ready_to_commit'
    assert saved['current'] == before.model_dump(mode='json')
    no_calls = ScriptedModel([])
    resumed, task = restart(tmp_path, saved, no_calls)
    resumed._run_task(task, 'commit')
    assert not no_calls.requests and len(resumed.session.history) == 1
    assert next(edge for edge in resumed.session.current.graph.edges if edge.id == 'e').properties['description'].startswith('A precise')


def test_failed_candidate_recompilation_preserves_saved_draft_candidate_and_acceptance(tmp_path, monkeypatch):
    saved = interrupted(tmp_path, [call('edit_draft', edit())])
    before_state = deepcopy(saved['task_state'])
    chat = ScriptedModel([])
    engine, task = restart(tmp_path, saved, chat)
    before = engine.session.current
    def reject(*args, **kwargs):
        raise ValueError('Controlled recompile rejection')
    monkeypatch.setattr('cortex.capabilities.graph_merge.reconciliation.compiler.compile_draft', reject)
    with pytest.raises(ValueError, match='Controlled recompile'):
        engine._run_task(task, 'recovery')
    assert not chat.requests and engine.session.current == before and not engine.session.history
    latest = WorkspaceStore(engine.workspace_path).load('recovery')['task_state']
    for key in ('draft', 'draft_id', 'last_valid_candidate', 'candidate_hash', 'candidate_draft_id'):
        assert latest[key] == before_state[key]
