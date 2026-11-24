"""Exercise the real scheduler, edit/compile/Assessment and SQLite boundary."""
import json
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage
from graph_merge_fixtures import catalog, concept, state
from graph_merge_chat import ScriptedModel
from graph_merge_runtime_fixtures import engine_for

from cortex.capabilities.graph_merge.reconciliation.receipts import is_accepted
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import build_contract_versions
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent


def check(ident='A'):
    return {'id': 'required:'+ident, 'kind': 'node_relation_followup', 'mode': 'required',
        'node_ids': [ident], 'observed': {'required_outcome':'Verify the existing representation preserves accepted meaning.'}}


def replies(n=1):
    result=[]
    for i in range(n):
        result.extend([AIMessage(content='',tool_calls=[{'name':'edit_draft','args':{},'id':f'edit{i}'}]),
            AIMessage(content=json.dumps({'explanation':'The unchanged candidate satisfies every required check and preserves its knowledge.','feedback':[]}))])
    return result


def make_engine(tmp_path,n=1):
    base=state([*catalog(),concept('A','a'),concept('B','b')])
    agent=IncrementalIntegrationAgent(ScriptedModel(replies(n)))
    engine,_=engine_for(base,agent,tmp_path/'workspace.sqlite')
    engine.contract_versions=build_contract_versions(engine.ctx.schema)
    return engine


def run(engine, checks=None, ident='accepted'):
    checks=checks or [check()]
    return engine._run_task(LocalTask(ident,'reconcile',{i for c in checks for i in c['node_ids']},checks=checks),'run')


def test_noop_accept_saves_per_check_receipts_in_graph_commit(tmp_path):
    engine=make_engine(tmp_path)
    run(engine,[check('A'),check('B')])
    saved=WorkspaceStore(engine.workspace_path).load('run')
    assert saved['task_state']['stage']=='committed'
    assert len(saved['signal_receipts'])==2
    assert len(saved['history'])==1
    assert all(row['candidate_hash']==saved['history'][0]['candidate_hash'] for row in saved['signal_receipts'])
    assert is_accepted(engine,check('A')) and is_accepted(engine,check('B'))


def test_same_check_id_cannot_silently_overwrite_different_obligation(tmp_path):
    engine=make_engine(tmp_path)
    first=check();second=deepcopy(first);second['observed']['required_outcome']='A distinct obligation'
    with pytest.raises(ValueError,match='Ambiguous reconciliation check ID'):
        run(engine,[first,second])
    assert not engine.history and not getattr(engine,'signal_receipts',[])


def test_failed_commit_rolls_back_receipts_and_resumes_without_model_calls(tmp_path):
    engine=make_engine(tmp_path)
    persist=engine._persist
    def fail(ident,*args,**kwargs):
        if engine.task_state.stage=='committed':
            raise OSError('Simulated atomic commit failure')
        return persist(ident,*args,**kwargs)
    engine._persist=fail
    with pytest.raises(OSError,match='atomic commit failure'):run(engine)
    assert not engine.history and not engine.signal_receipts
    before=WorkspaceStore(engine.workspace_path).load('run')
    assert before['signal_receipts']==[] and before['task_state']['stage']=='ready_to_commit'
    engine._persist=persist
    # Scripted replies have already been exhausted: replaying either model fails.
    run(engine)
    assert len(engine.history)==1 and len(engine.signal_receipts)==1
    assert is_accepted(engine,check())


def test_process_restart_after_commit_needs_no_scheduler_coverage_save(tmp_path,monkeypatch):
    engine=make_engine(tmp_path)
    run(engine)
    saved=WorkspaceStore(engine.workspace_path).load('run')
    assert not saved['relation_coverage']
    replacement=make_engine(tmp_path)
    replacement.current=engine.current
    replacement.history=deepcopy(saved['history'])
    replacement.signal_receipts=deepcopy(saved['signal_receipts'])
    replacement.contract_versions=engine.contract_versions
    replacement.task_state=None
    # Simulate the post-load scheduler with no available model replies.
    replacement.agent=IncrementalIntegrationAgent(ScriptedModel([]))
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.inspection_signals',lambda *a,**k:[check()])
    replacement._reconcile('run',initial_targets={'A'})
    assert replacement.history==engine.history


def test_regrouping_filters_accepted_check_before_creating_new_task(tmp_path,monkeypatch):
    engine=make_engine(tmp_path,n=2)
    run(engine,[check('A')])
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.inspection_signals',lambda *a,**k:[check('A'),check('B')])
    observed=[]
    real=engine._run_task
    def capture(task,ident,**kwargs):
        observed.append(task)
        return real(task,ident,**kwargs)
    engine._run_task=capture
    engine._reconcile('run',initial_targets={'A','B'})
    assert len(observed)==1
    assert observed[0].checks==[check('B')]
    assert observed[0].id.startswith('structure-')
    assert is_accepted(engine,check('A')) and is_accepted(engine,check('B'))


def test_changed_premise_and_changed_required_outcome_are_not_suppressed(tmp_path):
    engine=make_engine(tmp_path)
    run(engine)
    changed=deepcopy(check());changed['observed']['required_outcome']='A new explicit obligation'
    assert not is_accepted(engine,changed)
    original=engine.current
    engine.current=original.model_copy(update={'graph':original.graph.model_copy(update={'nodes':tuple(
        n.model_copy(update={'description':'A new relevant condition'}) if n.id=='A' else n for n in original.graph.nodes)})})
    assert not is_accepted(engine,check())


def test_changed_premise_really_runs_writer_and_assessment_again(tmp_path,monkeypatch):
    engine=make_engine(tmp_path,n=2);run(engine)
    original=engine.current
    engine.current=original.model_copy(update={'graph':original.graph.model_copy(update={'nodes':tuple(
        n.model_copy(update={'description':'A new accepted relevant condition'}) if n.id=='A' else n for n in original.graph.nodes)})})
    # This accepted upstream change is a new transaction, separate from the
    # receipt-bearing reconciliation; model re-entry must not be suppressed.
    engine._revision+=1
    engine.history.append({'task_id':'upstream','phase':'consolidate','revision':engine._revision})
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.inspection_signals',lambda *a,**k:[check()])
    observed=[];real=engine._run_task
    def capture(task,ident,**kwargs):
        observed.append(task)
        return real(task,ident,**kwargs)
    engine._run_task=capture
    engine._reconcile('run',initial_targets={'A'})
    assert len(observed)==1 and observed[0].checks==[check()]
    assert len(engine.signal_receipts)==2 and is_accepted(engine,check())


def test_signal_closed_by_previous_queued_task_is_not_sent_to_models(tmp_path,monkeypatch):
    engine=make_engine(tmp_path)
    change={'put_changes':[{'change_id':'clarify','kind':'node_update','node_id':'A',
        'set':{'description':'This accepted change closes the queued check.'},'reason':'Resolve coupled issue.'}]}
    engine.agent=IncrementalIntegrationAgent(ScriptedModel([
        AIMessage(content='',tool_calls=[{'name':'edit_draft','args':change,'id':'edit'}]),replies()[1]]))
    def signals(ctx,preview,**kwargs):
        a=next(n for n in preview.state.graph.nodes if n.id=='A')
        return [check('A')] if a.description else [check('A'),check('B')]
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.inspection_signals',signals)
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.review_batches',
        lambda current,checks,**kwargs:[(set(c['node_ids']),[c]) for c in checks])
    engine._reconcile('run',initial_targets={'A','B'})
    assert len(engine.history)==1 and is_accepted(engine,check('A'))
    assert not is_accepted(engine,check('B'))


def test_acceptance_is_bound_to_after_not_fixed_before(tmp_path):
    engine=make_engine(tmp_path)
    change={'put_changes':[{'change_id':'description','kind':'node_update','node_id':'A',
        'set':{'description':'A complete retained description.'},'reason':'Clarify the same evidenced meaning.'}]}
    engine.agent=IncrementalIntegrationAgent(ScriptedModel([
        AIMessage(content='',tool_calls=[{'name':'edit_draft','args':change,'id':'edit'}]),replies()[1]]))
    before=engine.current
    run(engine)
    after=engine.current
    assert after!=before and is_accepted(engine,check())
    engine.current=before
    assert not is_accepted(engine,check())
    engine.current=after


@pytest.mark.parametrize('broken',[None,{},[{}]])
def test_workspace_refuses_malformed_present_ledger(tmp_path,broken):
    engine=make_engine(tmp_path);run(engine)
    saved=WorkspaceStore(engine.workspace_path).load('run')
    saved['signal_receipts']=broken
    with pytest.raises(ValueError,match='[Rr]eceipt|ledger'):
        WorkspaceStore(tmp_path/'broken.sqlite').save('run',saved)


def test_legacy_absence_is_not_fabricated_from_old_batch_coverage(tmp_path):
    engine=make_engine(tmp_path);run(engine)
    saved=WorkspaceStore(engine.workspace_path).load('run')
    saved.pop('signal_receipts')
    for h in saved['history']:
        h.pop('reconciliation_receipt_policy_hash',None)
        h.pop('reconciliation_check_hashes',None)
    saved['relation_coverage']=['old-whole-region-hash']
    store=WorkspaceStore(tmp_path/'legacy.sqlite');store.save('run',saved)
    legacy=store.load('run')
    assert 'signal_receipts' not in legacy
    engine.signal_receipts=[]
    assert not is_accepted(engine,check())


def test_new_checkpoint_cannot_silently_lose_entire_ledger(tmp_path):
    engine=make_engine(tmp_path);run(engine)
    saved=WorkspaceStore(engine.workspace_path).load('run')
    saved.pop('signal_receipts')
    with pytest.raises(ValueError,match='[Mm]issing.*receipt|receipt.*[Mm]issing'):
        WorkspaceStore(tmp_path/'missing.sqlite').save('run',saved)


def test_full_merge_restart_after_durable_accept_before_scheduler_progress(tmp_path,monkeypatch):
    from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
    from cortex.capabilities.graph_merge.identity.contract import IdentityDecision
    from cortex.schema import load_root_schema
    class Agent:
        reconciliation_contract=True
        def __init__(self,responses):
            self.delegate=IncrementalIntegrationAgent(ScriptedModel(responses))
            self.phases=[]
        def invoke(self,phase,context,payload):
            self.phases.append(phase)
            if phase=='identity':return IdentityDecision()
            return self.delegate.invoke(phase,context,payload)
    def signals(ctx,preview,**kwargs):
        ident=next(n.id for n in preview.state.graph.nodes if n.node_type=='concept')
        return [check(ident)]
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.inspection_signals',signals)
    source=state([*catalog(),concept('A','a')]);schema=load_root_schema()
    engine=IncrementalGraphIntegrationEngine(Agent(replies()))
    engine.workspace_path=tmp_path/'full.sqlite'
    persist=engine._persist
    def crash_after_save(ident,*args,**kwargs):
        persist(ident,*args,**kwargs)
        if engine.task_state is not None and engine.task_state.stage=='committed':
            raise OSError('Process died after atomic accepted commit')
    engine._persist=crash_after_save
    with pytest.raises(OSError,match='atomic accepted commit'):
        engine.merge(state(),source,schema,run_id='full')
    saved=WorkspaceStore(engine.workspace_path).load('full')
    assert saved['task_state']['stage']=='committed' and len(saved['signal_receipts'])==1
    assert not saved['reconciliation_complete']
    agent=Agent([]);resumed=IncrementalGraphIntegrationEngine(agent)
    resumed.workspace_path=engine.workspace_path;resumed.resume_snapshot=saved
    result=resumed.merge(state(),source,schema,run_id='full')
    assert agent.phases==[]
    assert resumed.current.model_dump(mode='json')==saved['current']
    assert any(n.node_type=='concept' for n in result.state.graph.nodes)
    assert resumed.history==saved['history']
