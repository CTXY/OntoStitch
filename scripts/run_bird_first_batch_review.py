"""Run exactly one ten-case batch per database, then stop for user review.

Separate new workspaces preserve legacy checkpoints. No publisher/latest pointer
is updated and no subsequent batch can be launched by this worker.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from html import escape
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.runtime.state import build_contract_versions, canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository, SqlTrainingGraphMergeEvidenceResolver
from cortex.capabilities.sql_training.merge_evaluation import compose_case_graphs, _augment_existing_physical_graph
from cortex.config import load_config
from cortex.config.models import LLMConfig
from cortex.infrastructure.artifacts.graph_html import render_graph_html
from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.llm.providers import create_chat_model
from cortex.schema import GraphState, GraphSpace, load_root_schema
from audit_incremental_merge import audit_result
from run_resilient_training import observe, transport_error_details

REPO=Path(__file__).resolve().parents[1]
DATABASES=('thrombosis_prediction','toxicology')
SLOTS={'thrombosis_prediction':[0,2,4],'toxicology':[1,3,5]}
PHYSICAL={'table','column','dimension_value'}


def read(path):return json.loads(Path(path).read_text())
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n');temp.replace(path)


def prepare(root):
    if (root/'plan.json').exists():raise ValueError('Plan already exists; use worker to resume, never overwrite inputs')
    source_manifest=read(REPO/'output/latest_graphs/manifest.json')['databases']
    plan={'created_at':datetime.now().astimezone().isoformat(),'mode':'fresh_ten_case_review',
          'batch_size':10,'max_batches_per_database':1,'stop_after_batch':'awaiting_user_review',
          'publish_latest':False,'databases':{},'source_sha256':{},
          'contract_versions':build_contract_versions(load_root_schema()).to_dict()}
    for db in DATABASES:
        row=source_manifest[db];ids=sorted(row['cases'],key=int)[:10]
        assert len(ids)==10
        cases=[];evidence={};selection=[]
        for case in ids:
            source=REPO/f'output/latest_graphs/{db}/case_level_graphs/{case}/state.json'
            assert sha(source)==row['cases'][case]['sha256']
            from cortex.capabilities.graph_merge.integration.semantic_notes import migrate_relation_notes
            state,notes=migrate_relation_notes(read(source));GraphManager(load_root_schema()).validate_state(state)
            qa=REPO/f'output/bird_merge_v4_live_20261119_214104/integration/{db}/cases/{case}/input.json'
            raw=read(qa);assert raw['source_id']==f'bird-{db}-{case}'
            evidence[raw['source_id']]={k:raw[k] for k in ('question','sql','evidence')}
            cases.append((raw['source_id'],state.graph))
            for path in (source,qa):plan['source_sha256'][str(path)]=sha(path)
            dest=root/db/'cases'/case;dest.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,dest/'state.json');shutil.copyfile(qa,dest/'input.json')
            selection.append({'case_id':case,'source_graph':str(source),'question':raw['question'],
                              'semantic_nodes':sum(n.node_type not in PHYSICAL for n in state.graph.nodes)})
        empty=GraphState(graph=GraphSpace())
        composition=compose_case_graphs(tuple(cases),empty)
        existing=_augment_existing_physical_graph(empty,composition.catalog)
        GraphManager(load_root_schema()).validate_state(existing)
        write(root/db/'existing_state.json',existing.model_dump(mode='json'))
        write(root/db/'incoming_state.json',composition.state.model_dump(mode='json'))
        write(root/db/'case_input_mappings.json',composition.mappings)
        write(root/db/'source_evidence.json',evidence)
        write(root/db/'selection.json',selection)
        plan['databases'][db]={'case_ids':ids,'remaining_cases_not_authorized':row['passed_cases']-10,
           'excluded_cases':row.get('excluded_cases',{}),'credential_slots':SLOTS[db],
           'initial_semantic_nodes':sum(n.node_type not in PHYSICAL for n in composition.state.graph.nodes),
           'input_fingerprint':canonical_hash([existing.model_dump(mode='json'),composition.state.model_dump(mode='json')])}
        write(root/db/'status.json',{'status':'prepared','case_ids':ids,'progress_at':time.time()})
    keys=read_key_slots(REPO/'configs/sql_training.keys')
    assert all(a<len(keys) for values in SLOTS.values() for a in values)
    assert not ({keys[i].api_key.get_secret_value() for i in SLOTS[DATABASES[0]]}&
                {keys[i].api_key.get_secret_value() for i in SLOTS[DATABASES[1]]})
    plan['disjoint_credentials_verified']=True
    write(root/'plan.json',plan);render(root)


def configured_model(folder,slot):
    load_dotenv(REPO/'.env')
    cfg=load_config(REPO/'configs/cortex.yaml').llm.resolve('graph_merge')
    keys=read_key_slots(REPO/'configs/sql_training.keys')
    settings=cfg.model_copy(update={'api_key':keys[slot].api_key,'enable_thinking':None,
        'reasoning_effort':None,'timeout':600,'max_retries':0})
    model=create_chat_model(LLMConfig.model_validate(settings.model_dump()))
    model.streaming=model.stream_usage=True
    model.metadata={**(getattr(model,'metadata',None) or {}),'credential_slot':slot,'anonymous_key_slot':slot}
    observe(model,folder)
    write(folder/'model_configuration.json',{'model':settings.model,'endpoint_host':urlparse(settings.base_url).hostname,
        'anonymous_key_slot':slot,'timeout_seconds':600,'sdk_retries':0,'streaming':True,
        'thinking_override':'omitted_for_gateway_compatibility','runtime_source':str(REPO),
        'selected_key_matches_client':model.openai_api_key.get_secret_value()==keys[slot].api_key.get_secret_value()})
    return model


def run_worker(root,db):
    plan=read(root/'plan.json');entry=plan['databases'][db];folder=root/db
    if read(folder/'status.json')['status']=='awaiting_user_review':return
    assert len(entry['case_ids'])==10 and plan['max_batches_per_database']==1
    before=GraphState.model_validate(read(folder/'existing_state.json'))
    incoming=GraphState.model_validate(read(folder/'incoming_state.json'))
    assert canonical_hash([before.model_dump(mode='json'),incoming.model_dump(mode='json')])==entry['input_fingerprint']
    resolver=SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(read(folder/'source_evidence.json')))
    schema=load_root_schema();current_versions=build_contract_versions(schema).to_dict()
    if current_versions!=plan['contract_versions']:raise ValueError('Runtime policy changed after preparation; preserve this run and explicitly migrate or prepare a new one')
    slots=entry['credential_slots'];used_slots=[slots[0]]
    model=configured_model(folder,used_slots[-1])
    agent=IncrementalIntegrationAgent(model,max_calls=16,request_max_characters=210000,
        trace_sink=FileSystemGraphMergeTraceSink(folder/'agents.jsonl'))
    def recover(error):
        from cortex.capabilities.graph_merge.llm.transport import _connection_failure
        if not _connection_failure(error) or len(used_slots)>=len(slots):return None
        slot=slots[len(used_slots)];used_slots.append(slot)
        with (folder/'transport_recovery.jsonl').open('a') as stream:
            stream.write(json.dumps({'next_anonymous_key_slot':slot,'time':time.time(),
                'errors':transport_error_details(error,[k.api_key.get_secret_value() for k in read_key_slots(REPO/'configs/sql_training.keys')])})+'\n')
        return configured_model(folder,slot)
    agent.recover_transport=recover
    engine=IncrementalGraphIntegrationEngine(agent,evidence_resolver=resolver,max_rounds=5,
        model_call_budget=220,sql_dialect='sqlite',node_context_max_characters=110000)
    engine.workspace_path=folder/'workspace.sqlite';engine.identity_trace_path=folder/'agents.jsonl'
    run_id='first10-review:'+db
    if engine.workspace_path.exists():engine.resume_snapshot=WorkspaceStore(engine.workspace_path).load(run_id)
    write(folder/'status.json',{'status':'running','pid':os.getpid(),'case_ids':entry['case_ids'],
          'progress_at':time.time(),'stop_after_this_batch':True,'credential_pool':slots})
    render(root)
    try:
        result=engine.merge(before,incoming,schema,run_id=run_id)
        write(folder/'result.json',result.model_dump(mode='json'))
        audit=audit_result(before,incoming,result,schema);write(folder/'audit.json',audit)
        if audit['status']!='passed':raise ValueError('Public source/reference preservation audit failed; result retained for inspection')
        GraphManager(schema).validate_state(result.state)
        write(folder/'merged_state.json',result.state.model_dump(mode='json'))
        (folder/'merged_graph.html').write_text(render_graph_html(result.state.graph,schema,title=db+' · first 10 cases · awaiting review'))
        write(folder/'status.json',{'status':'awaiting_user_review','case_ids':entry['case_ids'],
            'model_calls':(agent.call_budget or {}).get('used'),'history':engine.history,
            'nodes':len(result.state.graph.nodes),'edges':len(result.state.graph.edges),
            'node_types':dict(Counter(n.node_type for n in result.state.graph.nodes)),
            'conflict_nodes':len(result.state.conflict_nodes),'progress_at':time.time(),
            'audit':audit['status'],'used_anonymous_key_slots':used_slots,'next_batch_started':False})
    except Exception as error:
        keys=[k.api_key.get_secret_value() for k in read_key_slots(REPO/'configs/sql_training.keys')]
        write(folder/'status.json',{'status':'needs_attention','case_ids':entry['case_ids'],'progress_at':time.time(),
            'errors':transport_error_details(error,keys),'checkpoint_preserved':engine.workspace_path.exists(),
            'used_anonymous_key_slots':used_slots,'next_batch_started':False})
        print(type(error).__name__,str(error)[:800],flush=True)
    finally:render(root)


def journal_summary(folder):
    path=folder/'workspace.sqlite'
    if not path.exists():return {}
    import sqlite3
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2) as db:
            row=db.execute('select payload from merge_workspaces order by revision desc limit 1').fetchone()
        if not row:return {}
        journal=json.loads(row[0]);task=journal.get('task_state') or {}
        history=journal.get('history',[])
        return {'accepted_tasks':len(history),'phase':task.get('phase'),'task_id':task.get('task_id'),
                'task_stage':task.get('stage'),'draft_id':task.get('draft_id'),
                'identity_groups':len((journal.get('identity') or {}).get('groups',[])),
                'planned_groups':len(journal.get('content_plan') or []),
                'accepted_node_tasks':sum(x.get('phase')=='consolidate' for x in history),
                'accepted_relation_tasks':sum(x.get('phase')=='reconcile' for x in history),
                'history':history,'current_counts':{k:len(journal['current']['graph'][k]) for k in ('nodes','edges')}}
    except (OSError,ValueError,sqlite3.Error):return {'checkpoint_read':'busy_or_not_yet_committed'}


def render(root):
    if not (root/'plan.json').exists():return
    plan=read(root/'plan.json');pieces=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30"><title>BIRD 首批 10 cases 合并 review</title><style>body{font:16px/1.7 system-ui;max-width:1200px;margin:30px auto;padding:0 20px;color:#1e293b}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f9;padding:15px;font-size:13px;max-height:65vh;overflow:auto}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cbd5e1;padding:9px}details{margin:15px 0}a{color:#0369a1}.note{padding:15px;background:#e0f2fe}</style><h1>两个数据库 · 首批 10 case-level graphs</h1>',
        '<p class="note">使用当前英文 prompts 和新 merge 流程，从已通过的原始 case-level graphs 重新构建。每个库仅一批 10 条；通过后停在 awaiting_user_review，用户确认前不会合并后续 cases。旧 merged graph、checkpoint 与 case 输入均保留。页面每 30 秒刷新。</p>']
    statuses={};finished=True
    for name,entry in plan['databases'].items():
        folder=root/name;status=read(folder/'status.json');progress=journal_summary(folder)
        events=[];trace=folder/'model_calls.jsonl'
        if trace.exists():
            for line in trace.read_text().splitlines():
                try:events.append(json.loads(line))
                except ValueError:continue
        starts=[e for e in events if e.get('event_type')=='model_start'];ends=[e for e in events if e.get('event_type')=='model_end']
        status['calls_by_role']=dict(Counter(e.get('component') for e in starts))
        status['observed_model_calls']=len(starts);status['observed_completed_calls']=len(ends)
        status['active_role']=starts[-1].get('component') if len(starts)>len(ends) and starts else None
        status['checkpoint_progress']=progress;statuses[name]=status
        finished&=status['status'] in {'awaiting_user_review','needs_attention'}
        pieces.extend(['<h2>'+escape(name)+' · '+escape(status['status'])+'</h2>',
            '<p>Cases: '+', '.join(entry['case_ids'])+'。后续 '+str(entry['remaining_cases_not_authorized'])+' 条未启动。</p>',
            '<p>实际请求 '+str(len(starts))+'，已返回 '+str(len(ends))+'；'+escape(str(status['active_role'] or '暂无未返回调用'))+'</p>'])
        if (folder/'merged_graph.html').exists():pieces.append('<p><a href="'+name+'/merged_graph.html?view=business">查看业务 merged graph</a> · <a href="'+name+'/merged_graph.html">完整图</a> · <a href="'+name+'/merged_state.json">Graph JSON</a> · <a href="'+name+'/audit.json">来源/引用核验</a></p>')
        pieces.append('<details open><summary>当前进度与 checkpoint</summary><pre>'+escape(json.dumps({k:v for k,v in status.items() if k!='history'},ensure_ascii=False,indent=2))+'</pre></details>')
        selection=read(folder/'selection.json');pieces.append('<details><summary>10 条原始问题与选取来源</summary><pre>'+escape(json.dumps(selection,ensure_ascii=False,indent=2))+'</pre></details>')
        pieces.append('<p><a href="'+name+'/source_evidence.json">原始 Question / Gold SQL / evidence</a> · <a href="'+name+'/incoming_state.json">合并前完整输入图</a></p>')
        if trace.exists():pieces.append('<p><a href="'+name+'/model_calls.jsonl">全部模型输入输出</a> · <a href="'+name+'/agents.jsonl">任务与编译记录</a></p>')
    unchanged=all(Path(path).is_file() and sha(path)==digest for path,digest in plan['source_sha256'].items())
    write(root/'progress.json',{'databases':statuses,'source_files_unchanged':unchanged,
        'review_gate_closed':True,'all_workers_stopped_or_waiting':finished,'updated':time.time()})
    pieces.append('<p>原始选中图与证据哈希未变：'+str(unchanged)+'；两进程凭据池互不重叠：'+str(plan['disjoint_credentials_verified'])+'。</p>')
    pieces.append('<details><summary>计划与契约版本</summary><pre>'+escape(json.dumps(plan,ensure_ascii=False,indent=2))+'</pre></details>')
    temp=root/('index.html.'+str(os.getpid())+'.tmp');temp.write_text(''.join(pieces)+'</html>');temp.replace(root/'index.html')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--mode',choices=['prepare','worker','monitor'],required=True)
    parser.add_argument('--database',choices=DATABASES)
    args=parser.parse_args();root=args.output.resolve()
    if args.mode=='prepare':prepare(root)
    elif args.mode=='worker':
        if not args.database:parser.error('--database is required for worker')
        run_worker(root,args.database)
    else:
        for _ in range(1440):
            render(root)
            if read(root/'progress.json')['all_workers_stopped_or_waiting']:break
            time.sleep(20)


if __name__=='__main__':main()
