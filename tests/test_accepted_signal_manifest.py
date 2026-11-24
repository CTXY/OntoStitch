"""Real AFTER evidence: a repair must not invalidate its own accepted receipt."""
from cortex.capabilities.graph_merge.runtime.session import MergeSession
from copy import deepcopy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from graph_merge_fixtures import catalog, concept, edge, state
from graph_merge_chat import ScriptedModel
from graph_merge_runtime_fixtures import engine_for

from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.reconciliation.receipts import is_accepted, premise_hash, policy_hash, record_acceptance
from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.runtime.task import accepted_signal_checks
from cortex.capabilities.graph_merge.runtime.contracts import build_contract_versions
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.schema import GraphState, load_root_schema


@pytest.fixture(scope='module')
def real():
    path=Path(__file__).parent/'fixtures/reconciliation_signal_receipts/accepted_manifest_failure.json.gz'
    with gzip.open(path,'rt') as f:data=json.load(f)
    graph=GraphState.model_validate(data['current'])
    assert canonical_hash(graph)==data['accepted_current_hash']
    ctx=SimpleNamespace(schema=load_root_schema(),evidence=tuple(EvidenceItem.model_validate(e) for e in data['evidence']),sql_dialect='sqlite')
    return data,graph,ctx


@pytest.mark.parametrize('index',[0,1])
def test_actual_duplicate_is_only_stale_trigger_membership(real,index):
    data,graph,ctx=real;pair=data['pairs'][index]
    engine=SimpleNamespace(session=MergeSession(inputs=ctx, current=graph))
    # Frozen v1 hashes remain historical evidence. The narrower v2 scope has
    # its own policy; old hashes are not silently recast as new receipts.
    historical = ['be1eceb8c238abb30186bd1d92b10247c33e2f0fce189c50f232dec0d5a66985',
                  '13de56c38c82f2918e5e0ede6a7a920d3108dc612bd818dc92da5d82e342d274']
    assert pair['original']['premise_hash'] == historical[index]
    assert premise_hash((engine).session,pair['original']['check']) != premise_hash((engine).session,pair['detected_later'])
    assert all(not ids for h in data['later_history'] for kind in h['delta'].values() for ids in kind.values())


@pytest.mark.parametrize('index',[0,1])
def test_record_actual_after_check_then_same_next_scan_reuses(real,index):
    data,graph,ctx=real;pair=data['pairs'][index]
    engine=SimpleNamespace(session=MergeSession(inputs=ctx, current=graph, contract_versions=build_contract_versions(ctx.schema), signal_receipts=[]), agent=SimpleNamespace(reconciliation_contract=True))
    checks=accepted_signal_checks(ctx,graph,[pair['original']['check']])
    assert checks==[pair['detected_later']]
    task=LocalTask('controlled-real-replay','reconcile',set(checks[0]['node_ids']),checks=checks)
    history={'task_id':task.id,'phase':'reconcile','revision':1,'assessment':'graph_assess',
        'candidate_hash':canonical_hash(graph),'reconciliation_receipt_policy_hash':policy_hash((engine).session),
        'reconciliation_check_hashes':[canonical_hash(c) for c in checks]}
    record_acceptance((engine).session,task,SimpleNamespace(agent_recovery={}),history)
    assert is_accepted((engine).session,pair['detected_later'])


def test_refresh_does_not_remove_explicit_requirement_or_rewrite_obligation(real):
    data,graph,ctx=real;check=deepcopy(data['pairs'][0]['original']['check'])
    check.update(mode='required',required_outcome='Preserve the complete patient-to-birthday relationship.')
    refreshed=accepted_signal_checks(ctx,graph,[check])[0]
    assert refreshed['mode']=='required' and refreshed['required_outcome']==check['required_outcome']
    obligation={'kind':'node_relation_followup','id':'required-fix','node_ids':check['node_ids'],
        'edge_ids':check['edge_ids'],'required_outcome':'Preserve original source assertions.'}
    assert accepted_signal_checks(ctx,graph,[obligation])==[obligation]


def test_real_changed_meaning_still_invalidates_after_refresh(real):
    data,graph,ctx=real;check=data['pairs'][0]['detected_later']
    original=premise_hash((SimpleNamespace(session=MergeSession(inputs=ctx, current=graph))).session,check)
    ident=check['node_ids'][0]
    changed=graph.model_copy(update={'graph':graph.graph.model_copy(update={'nodes':tuple(
        n.model_copy(update={'description':(n.description or '')+' A new substantive condition.'}) if n.id==ident else n for n in graph.graph.nodes)})})
    assert premise_hash((SimpleNamespace(session=MergeSession(inputs=ctx, current=changed))).session,check)!=original


def test_real_commit_with_edge_replacement_does_not_reprocess_surviving_triangle(tmp_path,monkeypatch):
    nodes=[*catalog(),*(concept(k,'a') for k in ('A','B','C'))]
    edges=[edge('AB','A','B','has_feature'),edge('BC','B','C','has_feature'),
        edge('old','A','C','has_feature').model_copy(update={'properties':{'description':'An earlier complete assertion.'}}),
        edge('keep','A','C','has_feature').model_copy(update={'properties':{'description':'The retained equivalent assertion.'}})]
    original=state(nodes,edges)
    edit={'put_changes':[{'change_id':'fold','kind':'edge_replace','edge_id':'old',
        'replacement_edge_id':'keep','reason':'The actual retained assertion preserves the earlier equivalent relation and sources.'}]}
    agent=IncrementalIntegrationAgent(ScriptedModel([
        AIMessage(content='',tool_calls=[{'name':'edit_draft','id':'edit','args':edit}]),
        AIMessage(content=json.dumps({'explanation':'The retained relationship preserves the earlier assertion and its sources; the triangle is valid.','feedback':[]}))]))
    engine,_=engine_for(original,agent,tmp_path/'workspace.sqlite')
    checks=[s for s in inspection_signals(engine.session.inputs,Preview(original,{}, {},[])) if s['kind']=='triangle']
    assert len(checks)==1 and 'old' in checks[0]['edge_ids']
    engine._run_task(LocalTask('repair','reconcile',{'A','B','C'},checks=checks),'run')
    current=[s for s in inspection_signals(engine.session.inputs,Preview(engine.session.current,{}, {},[])) if s['kind']=='triangle']
    assert 'old' not in current[0]['edge_ids']
    assert engine.session.signal_receipts[0]['check']==current[0] and is_accepted((engine).session,current[0])
    assert engine.session.history[-1]['reconciliation_trigger_check_hashes']!=engine.session.history[-1]['reconciliation_check_hashes']
    monkeypatch.setattr('cortex.capabilities.graph_merge.engine.inspection_signals',lambda *a,**k:current)
    engine.agent=IncrementalIntegrationAgent(ScriptedModel([]))
    accepted=deepcopy(engine.session.history)
    engine._reconcile('run',initial_targets={'A','B','C'})
    assert engine.session.history==accepted
