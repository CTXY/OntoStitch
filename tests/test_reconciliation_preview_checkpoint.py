"""Explicit complete edits survive every compile/handoff crash window."""
from copy import deepcopy
import pytest
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from test_merge_task_state import graph, versions
from test_reconciliation_agent_runtime import CanonicalChat, fixture, call, edit


def restart(tmp_path, saved, chat):
    engine, task = fixture(tmp_path, chat)
    engine.session.task_state = TaskState.from_dict(saved['task_state'])
    engine.session.contract_versions = engine.session.task_state.versions
    engine.session.storage_revision = saved['revision']
    engine.session.history = deepcopy(saved['history'])
    engine.session.revision = saved['accepted_revision']
    return engine, task


@pytest.mark.parametrize('empty',[False,True])
@pytest.mark.parametrize('window',['action_saved','compiled','assessing'])
def test_authored_action_restart_does_not_repeat_writer(tmp_path,empty,window):
    chat=CanonicalChat(edit_edge=not empty)
    engine,task=fixture(tmp_path,chat)
    completed={'task_id':'previously-completed','revision':7,'assessment':'graph_assess'}
    engine.session.history=[completed];engine.session.revision=7
    original=engine._persist; stopped=False
    def persist(run_id):
        nonlocal stopped
        original(run_id)
        state=engine.session.task_state
        intent=state.agent_recovery.get('complete_draft_action')
        match=(window=='action_saved' and intent is not None or
            window=='compiled' and intent is not None and state.stage=='compiled_unsubmitted' or
            window=='assessing' and state.stage=='assessing')
        if match and not stopped:
            stopped=True
            raise RuntimeError('Power lost in '+window)
    engine._persist=persist
    with pytest.raises(RuntimeError,match='Power lost'):
        engine._run_task(task,'crash')
    saved=WorkspaceStore(engine.workspace_path).load('crash')
    assert saved['history']==[completed]
    assert saved['current']==engine.session.current.model_dump(mode='json')
    resumed_chat=CanonicalChat(edit_edge=not empty)
    resumed,resumed_task=restart(tmp_path,saved,resumed_chat)
    resumed._run_task(resumed_task,'crash')
    assert len(resumed_chat.requests)==1
    assert all(t['function']['name']!='edit_draft' for t in resumed_chat.requests[0][1])
    assert resumed.session.history[0]==completed and len(resumed.session.history)==2
    assert resumed.session.revision==8


def test_initial_empty_preview_still_requires_writer_judgment(tmp_path):
    chat=CanonicalChat();engine,task=fixture(tmp_path,chat)
    original=engine._persist;stopped=False
    def persist(run_id):
        nonlocal stopped
        original(run_id)
        if not stopped and engine.session.task_state.stage=='compiled_unsubmitted':
            stopped=True
            raise RuntimeError('Baseline preview only')
    engine._persist=persist
    with pytest.raises(RuntimeError,match='Baseline preview'):
        engine._run_task(task,'baseline')
    saved=WorkspaceStore(engine.workspace_path).load('baseline')
    assert 'complete_draft_action' not in saved['task_state']['agent_recovery']
    resumed_chat=CanonicalChat();resumed,resumed_task=restart(tmp_path,saved,resumed_chat)
    resumed._run_task(resumed_task,'baseline')
    assert len(resumed_chat.requests)==2
    assert any(t['function']['name']=='edit_draft' for t in resumed_chat.requests[0][1])


def test_failed_compile_restart_keeps_draft_and_error_feedback(tmp_path):
    class Invalid(CanonicalChat):
        def invoke(self,messages,config=None):
            if self.writer:
                self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return call('edit_draft',edit(target='missing-endpoint'))
            return super().invoke(messages,config)
    chat=Invalid();engine,task=fixture(tmp_path,chat)
    original=engine._persist;stopped=False
    def persist(run_id):
        nonlocal stopped
        original(run_id)
        if not stopped and engine.session.task_state.issues:
            stopped=True;raise RuntimeError('Failed compile saved')
    engine._persist=persist
    with pytest.raises(RuntimeError,match='Failed compile saved'):
        engine._run_task(task,'invalid')
    saved=WorkspaceStore(engine.workspace_path).load('invalid')
    assert saved['task_state']['issues']
    class Repair(CanonicalChat):
        def invoke(self,messages,config=None):
            if self.writer:
                self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                rt=self.engine.execution.context.reconciliation_runtime
                assert rt.draft.version==1
                assert 'missing-endpoint' in str(rt.feedback['latest_receipt']['errors'])
                replacement=edit();replacement['put_changes'][0]['change_id']='corrected'
                replacement['remove_changes']=['clarify']
                return call('edit_draft',replacement)
            return super().invoke(messages,config)
    repaired=Repair();resumed,resumed_task=restart(tmp_path,saved,repaired)
    resumed._run_task(resumed_task,'invalid')
    assert len(repaired.requests)==2 and len(resumed.session.history)==1


def test_exact_candidate_binding_is_required_before_assessment():
    original=TaskState.capture(graph(),task_id='task',phase='reconcile',versions=versions())
    preview=original.with_draft({'version':1}).with_candidate(graph('candidate'),submitted=False,compilation_binding='bound')
    assert TaskState.from_dict(preview.to_dict())==preview
    with pytest.raises(ValueError,match='Review does not address'):
        preview.with_review({'verdict':'accept','issues':[]},candidate_hash=preview.candidate_hash)
    assert preview.with_assessment_handoff().stage=='assessing'
    with pytest.raises(ValueError,match='DRAFT_NOT_COMPILED'):
        preview.with_draft({'version':2}).with_assessment_handoff()
    corrupt=preview.to_dict();corrupt['candidate_draft_id']=None
    with pytest.raises(ValueError,match='exact current candidate binding'):
        TaskState.from_dict(corrupt)
