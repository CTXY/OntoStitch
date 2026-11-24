"""Validate an explicit bounded failure set, preserving completed case results."""
from pathlib import Path
import sys
RUNTIME = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(RUNTIME/'src'), str(RUNTIME/'scripts'), str(RUNTIME/'baseline/src')]
import argparse, asyncio, hashlib, json, os, shutil, sqlite3, time
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from html import escape
from pydantic import SecretStr
from dotenv import load_dotenv
from cortex.config import load_config
from cortex.bootstrap.online_context import build_online_context_service, build_simulated_user_agent
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.adaptive_limit import AdaptiveRequestGate, install_adaptive_gate
from cortex.infrastructure.online_context import FileSystemGraphStateLoader, DatabaseDescriptionLoader
from cortex.application.online_context import calculate_graph_state_fingerprint
from cortex.capabilities.online_context.workflow.migration import checkpoint_values, migrate_recalled_state
from cortex.capabilities.retrieve_db.retrieval.selection import KeepRecalledSelector
from cortex.infrastructure.workflow.diagnostics import case_outcome, classify_workflow_error
from run_bird_dev_online_guided_baseline import _online_case, _guidance_case, _question_with_evidence
from run_graph_guided_test import baseline, CallAudit, audit_http, write, read


def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def now(): return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def stage_contract(runtime, config, model):
    files = {}
    for directory in ('src/cortex/capabilities/query_signals',):
        for f in sorted((runtime/directory).rglob('*')):
            if f.is_file() and f.suffix in ('.py','.md'): files[str(f.relative_to(runtime))]=sha(f)
    for relative in ('src/cortex/capabilities/retrieve_db/retrieval/recall.py',
                     'src/cortex/capabilities/retrieve_db/retrieval/index.py'):
        files[relative]=sha(runtime/relative)
    return {'files':files,'model':model,'reasoning_effort':'none',
            'temperature':config.llm.resolve('retrieve_db').temperature,
            'effective_embeddings':None,
            'limits':{k:getattr(config.services.retrieve_db,k+'_limit') for k in ('concept','metric','generation_rule','column','dimension_value')}}


def publish(out, state):
    state['updated_at']=now();write(out/'status.json',state)
    h='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15"><title>Online修复小规模验证</title><style>body{font:15px/1.65 system-ui;margin:25px;color:#16334a;background:#f6f8fb}table{border-collapse:collapse;background:white;width:100%}td,th{border:1px solid #ccd;padding:10px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}.note{background:#fff1cb;padding:16px}</style><body><h1>Online修复小规模验证</h1>'
    h+='<p class="note">全量运行保持暂停。分批验证14条指定失败case，已有通过结果直接保留；原始数据、baseline及checkpoint保留，通过结果经审计回填完整test集。官方接口、实际HTTP上限1、显式none/disabled；SQL执行失败最多修复2次。</p>'
    h+='<p>'+escape(state['updated_at'])+' · '+escape(state['status'])+'</p><table><tr><th>Case</th><th>迁移复用</th><th>验证状态</th><th>Baseline</th><th>Guided</th><th>错误</th></tr>'
    for key,v in state['cases'].items():
        result=v.get('result',{});plain=result.get('plain') or {};guided=result.get('guided') or {}
        cells=[key,', '.join(v.get('migration',{}).get('reused_stages',[])),v.get('status',''),str(plain.get('evaluation',{}).get('exec','—')),str(guided.get('evaluation',{}).get('exec','—')),str(result.get('error_code') or result.get('error') or '')]
        h+='<tr>'+''.join('<td>'+escape(c)+'</td>' for c in cells)+'</tr>'
    h+='</table><p>阶段进度：</p><pre>'+escape(json.dumps(state.get('current'),ensure_ascii=False,indent=2))+'</pre><p>核验：</p><pre>'+escape(json.dumps(state.get('verification',{}),ensure_ascii=False,indent=2))+'</pre><p><a href="status.json">状态JSON</a> · <a href="source_preservation.json">原始文件保留校验</a> · <a href="runtime_manifest.json">代码快照</a></p></body></html>'
    tmp=out/'index.html.tmp';tmp.write_text(h);tmp.replace(out/'index.html')


def configure(parent, protocol, key, keyfile, db, sid, folder):
    job=next(j for j in protocol['databases'] if j['database_id']==db)
    config=load_config(Path(job['folder'])/'config.yaml')
    cc=config.model_copy(update={
        'llm':config.llm.model_copy(update={'api_key':SecretStr(key),'base_url':'https://api.deepseek.com','model':protocol['model'],'max_retries':0,'enable_thinking':False,'reasoning_effort':'none'}),
        'services':config.services.model_copy(update={
            'online_context':config.services.online_context.model_copy(update={'checkpoint_path':folder/'online.sqlite','query_example_top_k':0}),
            'retrieve_db':config.services.retrieve_db.model_copy(update={'workspace':folder/'retrieval','embedding_model':None})})})
    return job, config, cc


async def main(args):
    parent=args.parent.resolve();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    assert read(parent/'user_pause.json')['status']=='paused_by_user'
    assert read(parent/'http_admission/state.json')['user_paused']
    maximum=getattr(args,'max_live_cases',5)
    if not 1<=maximum<=14 or len(args.case)>maximum or len(set(args.case))!=len(args.case):raise ValueError('Explicit distinct case list must respect its bounded live-case limit (at most14)')
    for name in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:os.environ.pop(name,None)
    os.environ['LANGSMITH_TRACING']=os.environ['LANGCHAIN_TRACING_V2']='false'
    load_dotenv('.env',override=False)
    protocol=read(parent/'protocol.json');provider=read(parent/'provider_override.json');keyfile=Path(provider['key_file']);key=keyfile.read_text().strip();fp=hashlib.sha256(key.encode()).hexdigest()[:12]
    assert fp==provider['key_fingerprint'] and provider['base_url']=='https://api.deepseek.com'
    source_runtime=Path(read(parent/'active_reasoning_none_resume.json')['runtime'])
    ledger=[json.loads(l) for l in (parent/'full_test_results.jsonl').read_text().splitlines()]
    failures={r['database_id']+'/'+str(r['source_id']):r for r in ledger if r.get('completed') and r.get('guided') is None}
    assert set(args.case)<=set(failures)
    watched=[parent/'full_test_results.jsonl',parent/'seed_full_test_results.jsonl']
    watched+=list(parent.glob('*/cases/*/*_prediction.json'))+list(parent.glob('*/cases/*/paired_result.json'))
    for keyid in failures:
        db,sid=keyid.split('/');watched+=list((parent/db/'cases'/sid).rglob('*.sqlite'))
    original=read(out/'source_preservation.json') if (out/'source_preservation.json').exists() else {'before':{str(f):sha(f) for f in watched}}
    write(out/'source_preservation.json',original)
    state={'status':'preparing_offline','full_run_remains_paused':True,'selected_live_cases':args.case,'cases':{},'current':None};publish(out,state)
    gate=AdaptiveRequestGate(out/'http_admission',max_limit=1)
    for identity,row in failures.items():
        db,sid=identity.split('/');folder=out/db/'cases'/sid;folder.mkdir(parents=True,exist_ok=True)
        job,config,cc=configure(parent,protocol,key,keyfile,db,sid,folder)
        case=next(json.loads(l) for l in (Path(job['folder'])/'test_cases.jsonl').read_text().splitlines() if str(json.loads(l)['source_id'])==sid)
        model=create_chat_model(cc.llm,profile='online_context')
        service=build_online_context_service(config=cc,llm=model,embeddings=None)
        assert isinstance(service._engine._retrieval._engine._workflow.nodes.selector,KeepRecalledSelector)
        graph=FileSystemGraphStateLoader().load(Path(job['folder'])/'merged_state.json')
        context=DatabaseDescriptionLoader().load(Path(protocol['database_root'])/db/'database_description')
        request={'question':_question_with_evidence(case).strip(),'graph_state':graph.model_dump(mode='json'),'graph_state_fingerprint':calculate_graph_state_fingerprint(graph),'dialect':'sqlite','initial_sql':None,'column_context':[c.model_dump(mode='json') for c in context],'question_budget':cc.services.online_context.question_budget}
        source_id=hashlib.sha256((protocol['runtime_manifest_sha256']+'|'+job['graph_sha256']+'|'+sid).encode()).hexdigest()
        target_id=hashlib.sha256((source_id+'|'+service._engine._workflow.identity).encode()).hexdigest()
        source=parent/db/'cases'/sid
        if not (folder/'migration.json').exists():
            archive=folder/'source_checkpoint_archive';archive.mkdir()
            for f in source.rglob('*.sqlite'):
                dest=archive/f.relative_to(source);dest.parent.mkdir(parents=True,exist_ok=True)
                with sqlite3.connect(f'file:{f.resolve()}?mode=ro',uri=True) as src,sqlite3.connect(dest) as dst:src.backup(dst);assert dst.execute('pragma quick_check').fetchone()[0]=='ok'
            for name in ['online.json','paired_result.json']:
                if (source/name).exists():shutil.copy2(source/name,archive/name)
            shutil.copy2(source/'plain_prediction.json',folder/'plain_prediction.json')
            receipt=await migrate_recalled_state(source_online_path=source/'online.sqlite',source_retrieval_root=source/'retrieval/checkpoints',target_engine=service._engine,source_run_id=source_id,target_run_id=target_id,expected_request=request,source_stage_contract=stage_contract(source_runtime,config,protocol['model']),target_stage_contract=stage_contract(RUNTIME,config,protocol['model']))
            write(folder/'migration.json',receipt)
        else:receipt=read(folder/'migration.json');assert receipt['target_identity']==service._engine._workflow.identity
        cached=read(folder/'case_result.json') if (folder/'case_result.json').exists() else None
        state['cases'][identity]={'status':cached['outcome'] if cached else 'prepared_offline','migration':receipt}
        if cached:state['cases'][identity]['result']=cached
        publish(out,state)
        await model.root_async_client.close();model.root_client.close()
    write(out/'offline_migration_summary.json',{'cases':state['cases'],'all_source_files_unchanged':all(sha(Path(f))==h for f,h in original['before'].items())})
    if not args.prepare_only:
        state['status']='running_selected_only';publish(out,state)
        for identity in args.case:
            assert read(parent/'http_admission/state.json')['user_paused']
            previous=state['cases'][identity].get('result')
            if previous and (previous.get('guided') or {}).get('evaluation',{}).get('exec')==1:
                continue
            db,sid=identity.split('/');folder=out/db/'cases'/sid;job,config,cc=configure(parent,protocol,key,keyfile,db,sid,folder)
            case=next(json.loads(l) for l in (Path(job['folder'])/'test_cases.jsonl').read_text().splitlines() if str(json.loads(l)['source_id'])==sid)
            model=create_chat_model(cc.llm,profile='online_context');model.callbacks=[CallAudit(folder)];audit_http(model,folder/'wire')
            def credential(req):
                assert req.url.host=='api.deepseek.com'
                assert hashlib.sha256(req.headers['authorization'].removeprefix('Bearer ').encode()).hexdigest()[:12]==fp
                payload=json.loads(req.content);assert payload['reasoning_effort']=='none' and payload['thinking']['type']=='disabled'
            async def async_credential(req):credential(req)
            model.root_client._client.event_hooks['request'].insert(0,credential);model.root_async_client._client.event_hooks['request'].insert(0,async_credential)
            install_adaptive_gate(model.root_client._client,gate);install_adaptive_gate(model.root_async_client._client,gate,asynchronous=True)
            traces=[]
            def trace(stage,payload):
                traces.append({'stage':stage,'payload':payload});state['current']={'case':identity,'stage':stage};publish(out,state)
            service=build_online_context_service(config=cc,llm=model,embeddings=None,trace_sink=trace)
            worker=SimpleNamespace(service=service,simulator=build_simulated_user_agent(config=cc,llm=model),traces=traces)
            graph=FileSystemGraphStateLoader().load(Path(job['folder'])/'merged_state.json');context=DatabaseDescriptionLoader().load(Path(protocol['database_root'])/db/'database_description')
            state['cases'][identity]['status']='running';publish(out,state)
            record=await _online_case(worker,graph_state=graph,column_context=context,case=case,run_id=read(folder/'migration.json')['target_run_id']);write(folder/'online.json',record)
            guided=None
            if record.get('guidance_content'):
                item=_guidance_case(case,record);write(folder/'guidance'/f'{sid}.json',item);state['current']={'case':identity,'stage':'guided_sql'};publish(out,state)
                guided=await asyncio.to_thread(baseline,case,item,'guided',folder,model,SimpleNamespace(database_root=Path(protocol['database_root']),model=protocol['model']))
            result={'case':identity,'online_status':record.get('status'),'plain':read(folder/'plain_prediction.json'),'guided':guided,'outcome':case_outcome(record,guided),'error_code':record.get('error_code'),'error_stage':record.get('error_stage'),'error':record.get('error')}
            write(folder/'case_result.json',result);state['cases'][identity].update(status=result['outcome'],result=result);publish(out,state)
            await model.root_async_client.close();model.root_client.close()
    unchanged=all(Path(f).exists() and sha(Path(f))==h for f,h in original['before'].items());assert unchanged
    original.update(checked_at=now(),all_unchanged=unchanged);write(out/'source_preservation.json',original)
    requests=list(out.glob('*/cases/*/wire/*_request.json'));responses=[read(f) for f in out.glob('*/cases/*/wire/*_response.json')]
    badfilter=[str(f) for f in requests if '"title": "RelevanceOutput"' in str(read(f)['body']['messages'])]
    results=[v['result'] for v in state['cases'].values() if v.get('result')]
    state.update(status='offline_prepared' if args.prepare_only else 'validation_complete',current=None,verification={'original_files_unchanged':unchanged,'offline_migrated_cases':len(state['cases']),'live_cases_completed':len(results),'guided_correct':sum(r.get('guided') is not None and r['guided'].get('evaluation',{}).get('exec')==1 for r in results),'online_blocked':sum(r['outcome']=='online_blocked' for r in results),'sql_errors':sum(r['outcome']=='sql_error' for r in results),'ea_mismatches':sum(r['outcome']=='ea_mismatch' for r in results),'http_requests':len(requests),'http_statuses':{str(code):sum(r['status']==code for r in responses) for code in {r['status'] for r in responses}},'reasoning_chars':sum(len(c.get('message',{}).get('reasoning_content') or '') for r in responses for c in r.get('body',{}).get('choices',[])),'actual_http_peak':read(gate.path)['peak_active'],'relevance_filter_requests':badfilter,'full_run_still_paused':read(parent/'http_admission/state.json')['user_paused']})
    assert not badfilter;publish(out,state);write(out/'summary.json',state);print(json.dumps(state['verification'],ensure_ascii=False),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--parent',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--case',action='append',default=[]);parser.add_argument('--max-live-cases',type=int,default=5);parser.add_argument('--prepare-only',action='store_true');args=parser.parse_args()
    try:asyncio.run(main(args))
    except BaseException as exc:
        args.output.mkdir(parents=True,exist_ok=True);write(args.output/'validation_error.json',classify_workflow_error(exc,stage='validation'));raise
