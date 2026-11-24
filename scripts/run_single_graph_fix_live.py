"""Bounded real-model regression: one node group and two single-graph tasks."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import json

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.schema import GraphState,GraphSpace,load_root_schema
from reconciliation_probe_fixtures import write
from run_node_consolidation_smoke import make_fixture,run_fixture,trace_summary
from run_relation_reconciliation_v2_smoke import model_for

REPO=Path(__file__).resolve().parents[1]
ROOT=REPO/'output/single_graph_merge_fix_20261121'


def args_for(output,slot):
    return SimpleNamespace(output=output,slot=slot,model='deepseek-v4-flash',timeout=180,
        config=REPO/'output/reconciliation_readable_prompts_20261121/compat_no_thinking.yaml',
        keys=REPO/'configs/sql_training.keys',max_calls=10,max_rounds=3,
        graph_root=REPO/'output/latest_graphs',source_input_root=REPO/'output/bird_merge_v4_live_20261119_214104/integration')


def node():
    args=args_for(ROOT/'live',7)
    name='node_patient';folder=args.output/name
    spec=(name,'thrombosis_prediction',('1153','1157'),'concept.patient')
    prepared=make_fixture(args,spec,load_root_schema())
    model=model_for(args,folder,7)
    result=run_fixture(args,spec,prepared,model,{'limit':18,'used':0})
    if result.get('node_assessment_passed'):
        graph=json.loads((folder/'after.json').read_text())
        assert not any('relation_metadata' in n['properties'] for n in graph['graph']['nodes'])
    write(folder/'result.json',{'name':name,'status':'passed' if result.get('node_assessment_passed') else 'unfinished',
          'node_assessment_passed':result.get('node_assessment_passed'),'fixture':result})
    return {'name':name,'status':result['status']}


def reconcile(database,owner,slot):
    args=args_for(ROOT/'live',slot);folder=args.output/database;folder.mkdir(parents=True,exist_ok=True)
    state=GraphState.model_validate_json((ROOT/database/'after.json').read_text())
    evidence=tuple(EvidenceItem.model_validate(e) for e in json.loads((ROOT/database/'evidence.json').read_text()))
    ctx=IntegrationContext(state,GraphState(graph=GraphSpace()),load_root_schema(),evidence=evidence)
    ctx.task_before=ctx.candidate=state
    agent=IncrementalIntegrationAgent(model_for(args,folder,slot),max_calls=10,
        request_max_characters=210000,trace_sink=FileSystemGraphMergeTraceSink(folder/'trace.jsonl'))
    agent.call_budget={'limit':20,'used':0}
    engine=IncrementalGraphIntegrationEngine(agent,max_rounds=3,model_call_budget=20)
    engine.ctx,engine.current,engine.lineage=ctx,state,LineageIndex(ctx)
    engine.history,engine.reviewed_sources,engine._revision=[],{},0
    engine.identity_decision,engine.open_tasks={},[]
    engine.workspace_path,engine._storage_revision=folder/'workspace.sqlite',None
    engine.pending_relation_work=engine.pending_review=None
    check={'id':'current-graph-role-check','kind':'node_relation_followup','mode':'required','node_ids':[owner],
        'required_outcome':'Check that the nominated current Concept and its physical realization express a valid business role and preserve matching methods, scope and evidence. This task concerns this owner and its incident relations; unrelated pre-existing binding contests are separate tasks. Keep correct existing content.'}
    task=LocalTask('single-graph:'+database,'reconcile',{owner},checks=[check])
    write(folder/'before.json',state.model_dump(mode='json'));write(folder/'checks.json',[check])
    result={'name':database,'status':'running','case_level_graph_reads':0,'source_archive_tools':False}
    try:
        engine._run_task(task,task.id)
        assert engine.history[-1]['assessment']=='graph_assess'
        saved=WorkspaceStore(engine.workspace_path).load(task.id)
        write(folder/'task_state.json',saved['task_state'])
        result.update(status='passed',independent_assessment=True,checkpoint_stage=saved['task_state']['stage'],
                      history=engine.history,model_calls=agent.call_budget['used'])
    except Exception as error:
        from run_resilient_training import transport_error_details
        result.update(status='unfinished',errors=transport_error_details(error,()))
    write(folder/'after.json',engine.current.model_dump(mode='json'))
    result['trace']=trace_summary(folder);write(folder/'result.json',result)
    return result


if __name__=='__main__':
    if (ROOT/'live/summary.json').exists():raise ValueError('Probe records already exist; preserve them and choose a fresh output root')
    (ROOT/'live').mkdir(exist_ok=True)
    with ThreadPoolExecutor(3) as pool:
        jobs=[pool.submit(node),pool.submit(reconcile,'thrombosis_prediction','input::n209',8),
              pool.submit(reconcile,'toxicology','input::n102',9)]
        results=[job.result() for job in jobs]
    write(ROOT/'live/summary.json',results)
    print(json.dumps([{'name':x['name'],'status':x['status']} for x in results]))
