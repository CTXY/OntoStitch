"""One explicitly requested SQL-only repeat of failed guided evaluations."""
from pathlib import Path
import sys

RUNTIME=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(RUNTIME/'src'),str(RUNTIME/'scripts'),str(RUNTIME/'baseline/src')]

import argparse,asyncio,fcntl,hashlib,json,os
from copy import deepcopy
from html import escape
from types import SimpleNamespace

from cortex.infrastructure.llm.adaptive_limit import AdaptiveRequestGate
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.workflow.diagnostics import case_outcome,classify_workflow_error
from run_guided_remaining import GuidedRun,CaseDeferred,WorkerKeyUnavailable,now,sha,jsonl
from run_graph_guided_test import baseline as generate_sql_arm,read,write
from run_bird_dev_online_guided_baseline import _online_case,_guidance_case,_question_with_evidence
from cortex.bootstrap.online_context import build_online_context_service,build_simulated_user_agent
from cortex.infrastructure.online_context import FileSystemGraphStateLoader,DatabaseDescriptionLoader
from cortex.application.online_context import calculate_graph_state_fingerprint
from cortex.capabilities.online_context.workflow.migration import migrate_recalled_state
from validate_online_recovery import stage_contract


class FormulaFailureRepeat(GuidedRun):
    def __init__(self,args):
        self.args=args;self.root=args.output.resolve();self.protocol=read(self.root/'protocol.json')
        self.source=Path(self.protocol['source_complete_run'])
        assert sha(self.source/'full_test_results.jsonl')==self.protocol['source_results_sha256']
        assert sha(self.root/'seed_full_test_results.jsonl')==self.protocol['seed_sha256']
        self.parent=Path(self.protocol['parent']);self.parent_protocol=read(self.parent/'protocol.json')
        self.provider=read(self.root/'provider_override.json');self.slots=read_key_slots(self.provider['keys_file'])
        self.fingerprints=[hashlib.sha256(s.api_key.get_secret_value().encode()).hexdigest()[:12] for s in self.slots]
        assert self.fingerprints==self.provider['key_fingerprints'] and args.workers<=len(self.slots)
        self.seed=[json.loads(line) for line in (self.root/'seed_full_test_results.jsonl').read_text().splitlines()]
        self.index={row['database_id']+'/'+str(row['source_id']):row for row in self.seed}
        assert len(self.protocol['selected_cases'])==31
        assert all(self.index[k]['guided']['evaluation']['exec']==0 for k in self.protocol['selected_cases'])
        self.http_policy=self.protocol['http_policy'];self.current={};self.disabled_workers=set()
        job=next(j for j in self.parent_protocol['databases'] if j['database_id']=='formula_1')
        assert sha(Path(job['folder'])/'merged_state.json')==job['graph_sha256']
        self.graph=FileSystemGraphStateLoader().load(Path(job['folder'])/'merged_state.json')
        self.contexts=DatabaseDescriptionLoader().load(Path(self.parent_protocol['database_root'])/'formula_1/database_description')
        self.finished=False;self.stop_reason=None;self.phase='prepared_sql_repeat'
        self.gate=AdaptiveRequestGate(self.root/'http_admission',max_limit=args.max_http,
            stages=tuple((6,30) for _ in range(args.max_http-1)),initial_limit=min(10,args.max_http),
            min_limit=min(2,args.max_http),decrease_factor=.5,failure_threshold=8,
            cooldown_seconds=5,auto_recover_seconds=90,isolate_credentials=True,
            allow_provider_reasoning=self.provider.get('allow_provider_reasoning',False),
            wire_request_mode=self.provider.get('wire_request_mode','explicit_none'))
        for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:os.environ.pop(key,None)
        os.environ['LANGSMITH_TRACING']=os.environ['LANGCHAIN_TRACING_V2']='false'

    async def prepare(self):
        for item in read(self.root/'guidance_sources.json'):
            assert sha(Path(item['snapshot']))==item['guidance_sha256']
        self.phase='running_sql_repeat';self.publish()

    async def one(self,key,worker):
        folder=self.folder(key)
        if (folder/'case_result.json').exists():return
        sid=key.split('/')[1];row=self.index[key]
        case={name:row.get(name,'') for name in ['source_id','database_id','question','evidence','sql']}
        _,_,cfg=self.config(key,worker);original_cfg=cfg
        batch_size=self.protocol.get('intra_batch_sizes',{}).get(key)
        fresh_native=key in self.protocol.get('fresh_online_cases',[])
        if batch_size:
            cfg=cfg.model_copy(update={'services':cfg.services.model_copy(update={
                'online_context':cfg.services.online_context.model_copy(update={'checkpoint_path':folder/('online_native_fresh.sqlite' if fresh_native else 'online_batched.sqlite')}),
                'retrieve_db':cfg.services.retrieve_db.model_copy(update={'workspace':folder/('retrieval_native_fresh' if fresh_native else 'retrieval_batched')})})})
        model=self.model(cfg,folder,worker)
        self.current[worker]={'case':key,'stage':'guided_sql_repeat','updated_at':now()}
        try:
            if key in self.protocol.get('regenerate_guidance_cases',[]):
                traces=[]
                def trace(stage,payload):
                    traces.append({'stage':stage,'payload':payload})
                    self.current[worker]={'case':key,'stage':stage,'updated_at':now()}
                options=({'discovery_intra_batch_size':batch_size,'discovery_checkpoint_root':folder/('discovery_batches_native_fresh' if fresh_native else 'discovery_batches')} if batch_size else {})
                service=build_online_context_service(config=cfg,llm=model,embeddings=None,trace_sink=trace,**options)
                run_id=hashlib.sha256((self.protocol['seed_sha256']+key+service._engine._workflow.identity+('fresh_native_v1' if fresh_native else '')).encode()).hexdigest()
                if batch_size and not fresh_native and not (folder/'batched_migration.json').exists():
                    expected={'question':_question_with_evidence(case).strip(),'graph_state':self.graph.model_dump(mode='json'),
                        'graph_state_fingerprint':calculate_graph_state_fingerprint(self.graph),'dialect':'sqlite','initial_sql':None,
                        'column_context':[c.model_dump(mode='json') for c in self.contexts],
                        'question_budget':cfg.services.online_context.question_budget}
                    receipt=await migrate_recalled_state(source_online_path=folder/'online.sqlite',
                        source_retrieval_root=folder/'retrieval/checkpoints',target_engine=service._engine,
                        source_run_id=read(folder/'execution.json')['run_id'],target_run_id=run_id,expected_request=expected,
                        source_stage_contract=stage_contract(Path(self.protocol['pre_batch_runtime']),original_cfg,self.protocol['model']),
                        target_stage_contract=stage_contract(RUNTIME,cfg,self.protocol['model']))
                    receipt['reason']='Intra discovery batching changed; reuse verified initial SQL and recall, preserve the original checkpoints'
                    write(folder/'batched_migration.json',receipt)
                write(folder/('execution_native_fresh.json' if fresh_native else 'execution_batched.json' if batch_size else 'execution.json'),{'run_id':run_id,'workflow_identity':service._engine._workflow.identity,'reason':'Latest guidance artifact missing; regenerate online context'})
                online_path=folder/('online_native_fresh.json' if fresh_native else 'online_batched.json' if batch_size else 'online.json')
                cached=read(online_path) if online_path.exists() else None
                if cached and cached.get('status')!='error':record=cached
                else:
                    context=SimpleNamespace(service=service,simulator=build_simulated_user_agent(config=cfg,llm=model),traces=traces)
                    record=await _online_case(context,graph_state=self.graph,column_context=self.contexts,case=case,run_id=run_id)
                    write(online_path,record);write(folder/('stage_traces_native_fresh.json' if fresh_native else 'stage_traces_batched.json' if batch_size else 'stage_traces.json'),traces)
                if record.get('status')=='error':
                    if self.worker_key_disabled(worker):raise WorkerKeyUnavailable('online credential unavailable')
                    raise CaseDeferred(record.get('error','Online context did not complete'))
                write(folder/'guidance'/f'{sid}.json',_guidance_case(case,record))
            guidance=read(folder/'guidance'/f'{sid}.json')
            pred=await asyncio.to_thread(generate_sql_arm,case,guidance,'guided',folder,model,
                SimpleNamespace(database_root=Path(self.parent_protocol['database_root']),
                                model=self.protocol['model'],generation_transport_attempts=1))
            if pred.get('error') and pred.get('error_stage')=='generation':
                if self.worker_key_disabled(worker):raise WorkerKeyUnavailable('credential unavailable')
                raise CaseDeferred('SQL generation interrupted; no completed repeat recorded')
            result={'case':key,'guided':pred,'outcome':case_outcome({},pred),
                    'old_exec':0,'round':2,'finished_at':now(),'baseline_new_requests':0}
            write(folder/'case_result.json',result)
            if (folder/'deferred.json').exists():
                (folder/'deferred.json').rename(folder/'deferred_resolved.json')
        finally:
            self.current.pop(worker,None)
            await model.root_async_client.close();model.root_client.close()

    def publish(self):
        assert sha(self.source/'full_test_results.jsonl')==self.protocol['source_results_sha256']
        completed={};deferred=[]
        for key in self.protocol['selected_cases']:
            f=self.folder(key)
            if (f/'case_result.json').exists():completed[key]=read(f/'case_result.json')
            elif (f/'deferred.json').exists():deferred.append(read(f/'deferred.json'))
        passed=[k for k,v in completed.items() if v['guided'].get('evaluation',{}).get('exec')==1]
        errors=[k for k,v in completed.items() if v['guided'].get('error')]
        mismatches=[k for k,v in completed.items() if not v['guided'].get('error') and k not in passed]
        k=len(passed);done=len(completed)
        status='complete' if done==31 else 'finished_with_pending_cases' if self.finished else self.phase
        report={'checked_at':now(),'status':status,'mode':'guided_failure_repeat_once','pid':os.getpid(),
            'guided_evaluated':done,'guided_correct':k,'selected_total':31,'remaining':31-done,
            'retry_accuracy':k/done if done else None,'old_formula_correct':56,'formula_total':87,
            'old_formula_ea':56/87,'combined_formula_correct':56+k,'combined_formula_ea':(56+k)/87,
            'formula_gain_percentage_points':100*k/87,'old_overall_ea':186/244,
            'combined_overall_correct':186+k,'combined_overall_ea':(186+k)/244,
            'newly_passed_cases':passed,'still_ea_mismatches':mismatches,'sql_errors':errors,
            'deferred':deferred,'baseline_new_requests':0,'metric_note':self.protocol['metric_note'],
            'workers':self.current,'http_admission':read(self.gate.path),'cache_usage':self.cache_usage(),
            'current_provider_cache_usage':self.cache_usage(current_provider_only=True)}
        inputs={item['case']:item for item in read(self.root/'guidance_sources.json')}
        groups={}
        for key in self.protocol['selected_cases']:
            item=inputs[key]
            name='online_regenerated' if item.get('requires_online_regeneration') else 'older_retained_guidance' if item.get('latest_result_directory_missing') else 'latest_retained_guidance'
            group=groups.setdefault(name,{'selected':0,'completed':0,'newly_passed':0})
            group['selected']+=1;group['completed']+=int(key in completed);group['newly_passed']+=int(key in passed)
        report['guidance_source_groups']=groups
        write(self.root/'status.json',report);write(self.root/'summary.json',report)
        jsonl(self.root/'repeat_results.jsonl',list(completed.values()))
        combined=deepcopy(self.seed)
        for row in combined:
            key=row['database_id']+'/'+str(row['source_id'])
            if key in completed:
                row['guided']=completed[key]['guided'];row['guided_result_folder']=str(self.folder(key))
                row['status']='guided_failure_repeat_completed'
                row['guided_repeat_provenance']={'round':2,'selected_by_prior_failure':True,'source_run':str(self.source)}
                row['result_sources']=deepcopy(row.get('result_sources') or {})
                artifact=self.folder(key)/'guided_prediction.json'
                row['result_sources']['guided']={'path':str(artifact),'sha256':sha(artifact)}
        assert sum(row['guided']['evaluation']['exec']==1 for row in combined)==186+k
        assert all(a.get('plain')==b.get('plain') for a,b in zip(combined,self.seed))
        jsonl(self.root/'comparison_full_test_results.jsonl',combined)
        h='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="15"><title>Formula_1 失败集第二次尝试</title><style>body{font:15px/1.65 system-ui;margin:28px;background:#f7f9fc;color:#17324a}table{border-collapse:collapse;width:100%;background:white}td,th{border:1px solid #ccd;padding:9px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}.note{background:#fff0d4;padding:14px}</style><body><h1>Formula_1：31条失败case第二次尝试</h1>'
        h+=f'<p>{report["checked_at"]} · {status} · 已完成 {done}/31；新通过 {k}；SQL错误 {len(errors)}。</p>'
        h+=f'<p>原结果 56/87＝{56/87:.2%}；保留原通过结果后 {(56+k)}/87＝{(56+k)/87:.2%}，提升 {100*k/87:.2f} 个百分点。全244条补充结果 {(186+k)}/244＝{(186+k)/244:.2%}。</p>'
        h+='<p class="note">这是根据旧结果筛选失败case后的第二次尝试，不能作为新的独立全量单次EA。只重新生成SQL，复用存档guidance；不向模型提供Gold或旧SQL错误反馈，只有真实执行报错才允许最多2次修复。旧结果和原56条通过记录保留，baseline调用0。部分最新实验目录已缺失，使用的guidance快照来源全部记录。</p>'
        h+='<p><a href="guidance_sources.json">Guidance来源</a> · <a href="repeat_results.jsonl">本轮31条结果</a> · <a href="comparison_full_test_results.jsonl">独立补充版完整test数据</a> · <a href="summary.json">统计JSON</a></p><table><tr><th>Case</th><th>状态</th><th>原SQL / 本轮SQL</th></tr>'
        for key in self.protocol['selected_cases']:
            result=completed.get(key);state='新通过' if key in passed else 'SQL错误' if key in errors else 'EA仍不匹配' if result else '待完成'
            old=self.index[key]['guided'].get('predicted_sql','');new=(result or {}).get('guided',{}).get('predicted_sql','')
            h+='<tr><td>'+escape(key)+'</td><td>'+state+'</td><td><details><summary>查看SQL</summary><pre>原SQL:\n'+escape(old)+'\n\n本轮SQL:\n'+escape(new)+'</pre></details></td></tr>'
        h+='</table><details><summary>HTTP与任务状态</summary><pre>'+escape(json.dumps({'http':report['http_admission'],'workers':self.current,'deferred':deferred},ensure_ascii=False,indent=2))+'</pre></details></body></html>'
        tmp=self.root/'index.html.tmp';tmp.write_text(h);tmp.replace(self.root/'index.html')
        write(self.root.parent/'active_formula_guided_failure_repeat.json',{'folder':str(self.root),'status':status,'completed':done,'newly_passed':k,'html':str(self.root/'index.html')})
        return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=15);parser.add_argument('--max-http',type=int,default=10)
    args=parser.parse_args()
    with (args.output/'.runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run=FormulaFailureRepeat(args)
        try:asyncio.run(run.run())
        except BaseException as exc:write(args.output/'fatal_error.json',classify_workflow_error(exc));raise


if __name__=='__main__':main()
