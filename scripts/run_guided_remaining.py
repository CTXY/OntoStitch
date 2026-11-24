"""Resume only missing guided evaluations; never call the plain-baseline arm."""
from pathlib import Path
import sys
RUNTIME=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(RUNTIME/'src'),str(RUNTIME/'scripts'),str(RUNTIME/'baseline/src')]
import argparse,asyncio,collections,fcntl,hashlib,json,os,sqlite3,time
from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from html import escape
from dotenv import load_dotenv
from urllib.parse import urlparse
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.bootstrap.online_context import build_online_context_service,build_simulated_user_agent
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.adaptive_limit import AdaptiveRequestGate,install_adaptive_gate
from cortex.infrastructure.online_context import FileSystemGraphStateLoader,DatabaseDescriptionLoader
from cortex.application.online_context import calculate_graph_state_fingerprint
from cortex.capabilities.online_context.workflow.migration import checkpoint_values,migrate_recalled_state
from cortex.capabilities.retrieve_db.retrieval.selection import KeepRecalledSelector
from cortex.infrastructure.workflow.diagnostics import classify_workflow_error,case_outcome
from run_bird_dev_online_guided_baseline import _online_case,_guidance_case,_question_with_evidence
from run_graph_guided_test import baseline as generate_sql_arm,CallAudit,audit_http,write,read,is_transport_failure
from validate_online_recovery import configure,stage_contract


def now():return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
def sha(f):return hashlib.sha256(Path(f).read_bytes()).hexdigest()
def ok(pred):return bool(pred and pred.get('evaluation',{}).get('exec')==1)
def jsonl(path,rows):
    tmp=path.with_suffix('.jsonl.tmp');tmp.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows));tmp.replace(path)


class TransportStopped(RuntimeError):pass
class CaseDeferred(TransportStopped):pass
class WorkerKeyUnavailable(TransportStopped):pass


class GuidedRun:
    def __init__(self,args):
        self.args=args;self.root=args.output.resolve();self.parent=args.parent.resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.parent_protocol=read(self.parent/'protocol.json')
        self.provider=read(self.root/'provider_override.json' if (self.root/'provider_override.json').exists() else self.parent/'provider_override.json')
        manifest=read(self.root/'runtime_manifest.json')
        assert manifest and all(sha(RUNTIME/name)==digest for name,digest in manifest.items())
        self.slots=read_key_slots(self.provider.get('keys_file',self.provider.get('key_file')))
        self.fingerprints=[hashlib.sha256(s.api_key.get_secret_value().encode()).hexdigest()[:12] for s in self.slots]
        assert self.fingerprints==self.provider['key_fingerprints']
        assert 1<=args.workers<=len(self.slots),'Each worker requires a distinct API key'
        self.key=self.slots[0].api_key.get_secret_value();self.fingerprint=self.fingerprints[0]
        for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:os.environ.pop(key,None)
        os.environ['LANGSMITH_TRACING']=os.environ['LANGCHAIN_TRACING_V2']='false'
        self.models={};self.current={};self.stop_reason=None;self.finished=False
        protocol_file=self.root/'protocol.json'
        if not protocol_file.exists():
            seed_path=self.parent/'full_test_results.jsonl';seed=seed_path.read_bytes();(self.root/'seed_full_test_results.jsonl').write_bytes(seed)
            summary=read(self.parent/'summary.json');rows=[json.loads(l) for l in seed.decode().splitlines()]
            pending=[r['database_id']+'/'+str(r['source_id']) for r in rows if not r.get('completed') and r.get('guided') is None]
            snapshots={str(f):sha(f) for f in self.parent.glob('*/cases/*/plain_prediction.json')}
            for row in rows:
                path=(row.get('result_sources',{}).get('plain') or {}).get('path')
                if path and Path(path).is_file():snapshots[path]=sha(path)
            protocol={'created_at':now(),'mode':'guided_only','parent':str(self.parent),'seed_sha256':hashlib.sha256(seed).hexdigest(),'selected_cases':pending,'total_test_cases':len(rows),'baseline_new_requests':0,'baseline_records_available':sum(r.get('plain') is not None for r in rows),'baseline_reference':{'correct':summary['totals']['baseline_correct_completed'],'denominator':summary['totals']['completed'],'ea':summary['totals']['baseline_ea_completed'],'note':'Historical completed-pair reference; no new baseline calls in this run'},'preserved_baseline_hashes':snapshots,'source_runtime':read(self.parent/'active_reasoning_none_resume.json')['runtime'],'runtime':str(RUNTIME),'model':self.parent_protocol['model'],'base_url':'https://api.deepseek.com','workers':args.workers,'max_http':args.max_http,'prompt_layout':'static instructions, dialect+schema, then time/examples/guidance/memory/evidence/question; correction appended last','sql_execution_policy':'initial SQL execution plus at most two corrections; never retry EA mismatch','online_relevance_filter':False,'cache_usage_fields':['prompt_cache_hit_tokens','prompt_cache_miss_tokens'],'provider_key_fingerprint':self.fingerprint}
            assert len(rows)==244 and len(pending)==129
            write(protocol_file,protocol)
        self.protocol=read(protocol_file);assert sha(self.root/'seed_full_test_results.jsonl')==self.protocol['seed_sha256']
        self.http_policy=self.protocol.get('http_policy',{})
        self.disabled_workers=set()
        assert self.protocol['mode']=='guided_only' and self.protocol['baseline_new_requests']==0
        self.seed=[json.loads(l) for l in (self.root/'seed_full_test_results.jsonl').read_text().splitlines()]
        self.index={r['database_id']+'/'+str(r['source_id']):r for r in self.seed}
        self.gate=AdaptiveRequestGate(self.root/'http_admission',max_limit=args.max_http,
            stages=tuple((6,30) for _ in range(args.max_http-1)),min_long_completions=2,
            allow_provider_reasoning=self.provider.get('allow_provider_reasoning',False),
            initial_limit=min(6,args.max_http),min_limit=min(2,args.max_http),decrease_factor=0.5,
            failure_threshold=8,cooldown_seconds=5,auto_recover_seconds=90,isolate_credentials=True,
            wire_request_mode=self.provider.get('wire_request_mode','explicit_none'))
        self.graphs={};self.contexts={}
        for job in self.parent_protocol['databases']:
            db=job['database_id'];folder=Path(job['folder']);assert sha(folder/'merged_state.json')==job['graph_sha256']
            self.graphs[db]=FileSystemGraphStateLoader().load(folder/'merged_state.json')
            self.contexts[db]=DatabaseDescriptionLoader().load(Path(self.parent_protocol['database_root'])/db/'database_description')
        self.live_cases={}

    def folder(self,key):
        db,sid=key.split('/');return self.root/db/'cases'/sid

    def verify_baseline_files(self):
        existing=0;recovered=[]
        for file,digest in self.protocol['preserved_baseline_hashes'].items():
            if Path(file).is_file():
                assert sha(Path(file))==digest,file
                existing+=1
            else:
                proof=self.protocol.get('baseline_snapshot_fallbacks',{}).get(file)
                assert proof,'Missing baseline artifact without a verified seed snapshot: '+file
                assert sha(Path(proof['snapshot']))==proof['snapshot_sha256']
                record=self.index[proof['case']]['plain']
                assert read(Path(proof['snapshot']))==record
                recovered.append({'original_path':file,'case':proof['case'],'snapshot':proof['snapshot']})
        return {'existing_baseline_files_unchanged':existing,'missing_historical_artifacts_recovered_from_seed':recovered,'all_baseline_record_values_preserved':True}

    def config(self,key,worker=0):
        db,sid=key.split('/');folder=self.folder(key);folder.mkdir(parents=True,exist_ok=True)
        credential=self.slots[worker].api_key.get_secret_value()
        job,original,cfg=configure(self.parent,self.parent_protocol,credential,Path(self.provider.get('keys_file',self.provider.get('key_file'))),db,sid,folder)
        cfg=cfg.model_copy(update={'llm':cfg.llm.model_copy(update={'base_url':self.provider['base_url']})})
        return job,original,cfg

    def model(self,cfg,folder,worker=0):
        model=create_chat_model(cfg.llm,profile='online_context')
        wire_mode=self.provider.get('wire_request_mode','explicit_none')
        if wire_mode=='native_disabled':
            assert model.reasoning_effort=='none'
            model.wire_native_thinking_only=True
        model.callbacks=[CallAudit(folder)];audit_http(model,folder/'wire')
        timeout=type(model.root_client._client.timeout)(connect=90,write=300,read=1800,pool=180)
        model.root_client=model.root_client.with_options(timeout=timeout,max_retries=0);model.client=model.root_client.chat.completions
        model.root_async_client=model.root_async_client.with_options(timeout=timeout,max_retries=0);model.async_client=model.root_async_client.chat.completions
        def guard(req):
            expected=urlparse(self.provider['base_url'])
            assert req.url.host==expected.hostname and req.url.scheme==expected.scheme
            assert hashlib.sha256(req.headers['authorization'].removeprefix('Bearer ').encode()).hexdigest()[:12]==self.fingerprints[worker]
            body=json.loads(req.content);assert body['thinking']['type']=='disabled'
            assert ('reasoning_effort' not in body) if wire_mode=='native_disabled' else body['reasoning_effort']=='none'
            req.extensions['worker_identity']={'worker':worker,'key_slot':worker+1,'key_fingerprint':self.fingerprints[worker],'url':str(req.url),'request_bytes':len(req.content),'body_sha256':hashlib.sha256(req.content).hexdigest(),'timeout':req.extensions.get('timeout'),'configured_reasoning_effort':model.reasoning_effort,'wire_request_mode':wire_mode}
            with (folder/'http_identity.jsonl').open('a') as f:f.write(json.dumps({'event':'request','at':now(),**req.extensions['worker_identity']})+'\n')
            if body['messages'][0].get('content','').startswith('You are an expert data analyst.'):
                text=body['messages'][1]['content'];prefix=text.split('# current_time')[0]
                write(folder/'sql_prefix_audit.json',{'system_sha256':hashlib.sha256(body['messages'][0]['content'].encode()).hexdigest(),'schema_prefix_sha256':hashlib.sha256(prefix.encode()).hexdigest(),'schema_before_variables':text.index('# database_schema')<text.index('# current_time'),'arm':'guided'})
        async def async_guard(req):guard(req)
        def response_identity(resp):
            with (folder/'http_identity.jsonl').open('a') as f:f.write(json.dumps({'event':'response','at':now(),'request_id':resp.request.extensions.get('local_audit_id'),'status':resp.status_code,**resp.request.extensions['worker_identity']})+'\n')
        async def async_response_identity(resp):response_identity(resp)
        model.root_client._client.event_hooks['request'].insert(0,guard);model.root_async_client._client.event_hooks['request'].insert(0,async_guard)
        model.root_client._client.event_hooks['response'].append(response_identity);model.root_async_client._client.event_hooks['response'].append(async_response_identity)
        retries=getattr(self,'http_policy',{}).get('max_http_retries',6)
        install_adaptive_gate(model.root_client._client,self.gate,max_retries=retries);install_adaptive_gate(model.root_async_client._client,self.gate,asynchronous=True,max_retries=retries)
        return model

    async def prepare(self):
        self.phase='preparing_checkpoints';self.publish()
        for key in self.protocol['selected_cases']:
            folder=self.folder(key)
            if (folder/'execution.json').exists():continue
            db,sid=key.split('/');job,original,cfg=self.config(key);graph=self.graphs[db];context=self.contexts[db]
            model=create_chat_model(cfg.llm,profile='online_context')
            try:
                service=build_online_context_service(config=cfg,llm=model,embeddings=None)
                assert isinstance(service._engine._retrieval._engine._workflow.nodes.selector,KeepRecalledSelector)
                source_id=hashlib.sha256((self.parent_protocol['runtime_manifest_sha256']+'|'+job['graph_sha256']+'|'+sid).encode()).hexdigest()
                run_id=hashlib.sha256((source_id+'|'+service._engine._workflow.identity).encode()).hexdigest()
                source=self.parent/db/'cases'/sid
                source_values=checkpoint_values(source/'online.sqlite',source_id) if (source/'online.sqlite').exists() else {}
                case=self.index[key]
                request={'question':_question_with_evidence(case).strip(),'graph_state':graph.model_dump(mode='json'),'graph_state_fingerprint':calculate_graph_state_fingerprint(graph),'dialect':'sqlite','initial_sql':None,'column_context':[c.model_dump(mode='json') for c in context],'question_budget':cfg.services.online_context.question_budget}
                receipt=None
                if source_values:
                    receipt=await migrate_recalled_state(source_online_path=source/'online.sqlite',source_retrieval_root=source/'retrieval/checkpoints',target_engine=service._engine,source_run_id=source_id,target_run_id=run_id,expected_request=request,source_stage_contract=stage_contract(Path(self.protocol['source_runtime']),original,self.protocol['model']),target_stage_contract=stage_contract(RUNTIME,original,self.protocol['model']))
                    write(folder/'migration.json',receipt)
                write(folder/'execution.json',{'run_id':run_id,'workflow_identity':service._engine._workflow.identity,'source_run_id':source_id,'migrated':bool(receipt),'baseline_arm_enabled':False})
            finally:await model.root_async_client.close();model.root_client.close()
        self.phase='running_guided_only';self.publish()

    async def one(self,key,worker):
        folder=self.folder(key)
        if (folder/'case_result.json').exists():return
        if (folder/'blocked.json').exists() and not self.args.retry_blocked:return
        db,sid=key.split('/');case=self.index[key];_,_,cfg=self.config(key,worker);model=self.model(cfg,folder,worker);traces=[]
        def trace(stage,payload):
            traces.append({'stage':stage,'payload':payload});self.current[worker]={'case':key,'stage':stage,'updated_at':now()}
        service=build_online_context_service(config=cfg,llm=model,embeddings=None,trace_sink=trace)
        assert service._engine._workflow.identity==read(folder/'execution.json')['workflow_identity']
        worker_context=SimpleNamespace(service=service,simulator=build_simulated_user_agent(config=cfg,llm=model),traces=traces)
        self.current[worker]={'case':key,'stage':'online','updated_at':now()}
        try:
            record=read(folder/'online.json') if (folder/'online.json').exists() else None
            if not record or record.get('status')=='error':
                transport_attempts=getattr(self,'http_policy',{}).get('workflow_transport_attempts',2)
                for transport_attempt in range(transport_attempts):
                    record=await _online_case(worker_context,graph_state=self.graphs[db],column_context=self.contexts[db],case=case,run_id=read(folder/'execution.json')['run_id'])
                    attempt=len(list(folder.glob('online_attempt_*.json')))+1;write(folder/f'online_attempt_{attempt}.json',record);write(folder/'online.json',record)
                    if (record.get('status')!='error' or not record.get('retryable')
                            or read(self.gate.path).get('circuit_open') or transport_attempt==transport_attempts-1):break
                    self.current[worker]={'case':key,'stage':'bounded_transport_retry_same_checkpoint','updated_at':now()}
            if record.get('status')=='error':
                if record.get('error_code') in ('provider_quota_error','authentication_error'):raise WorkerKeyUnavailable(record['error'])
                if record.get('retryable'):raise CaseDeferred(record['error'])
                write(folder/'blocked.json',{'case':key,'outcome':'online_blocked','error':record.get('error'),'error_code':record.get('error_code'),'error_stage':record.get('error_stage'),'at':now()});return
            item=_guidance_case(case,record);write(folder/'guidance'/f'{sid}.json',item)
            self.current[worker]={'case':key,'stage':'guided_sql','updated_at':now()}
            # This is the sole downstream SQL-generation call. No plain arm exists.
            prediction=await asyncio.to_thread(generate_sql_arm,case,item,'guided',folder,model,SimpleNamespace(database_root=Path(self.parent_protocol['database_root']),model=self.protocol['model'],generation_transport_attempts=1))
            if prediction.get('error_stage')=='generation' and prediction.get('error'):
                write(folder/'interrupted.json',{'case':key,'status':'generation_interrupted','at':now(),'error':prediction.get('error'),'sql_attempts_preserved':len(prediction.get('sql_attempts') or [])})
                if self.worker_key_disabled(worker):raise WorkerKeyUnavailable('guided credential unavailable')
                raise CaseDeferred('guided generation interrupted; result excluded from EA')
            result={'case':key,'guided':prediction,'online_status':record.get('status'),'outcome':case_outcome(record,prediction),'baseline_new_requests':0,'finished_at':now()};write(folder/'case_result.json',result)
            if (folder/'blocked.json').exists():(folder/'blocked.json').rename(folder/f'blocked_resolved_{time.time_ns()}.json')
            if (folder/'interrupted.json').exists():(folder/'interrupted.json').rename(folder/f'interrupted_resolved_{time.time_ns()}.json')
            if (folder/'deferred.json').exists():(folder/'deferred.json').rename(folder/f'deferred_resolved_{time.time_ns()}.json')
        except TransportStopped:
            raise
        except Exception as exc:
            error=classify_workflow_error(exc)
            if error['error_code'] in ('provider_quota_error','authentication_error'):raise WorkerKeyUnavailable(error['safe_message']) from exc
            if error['retryable']:raise CaseDeferred(error['safe_message']) from exc
            write(folder/'blocked.json',{'case':key,'outcome':'online_blocked',**error,'at':now()})
        finally:
            write(folder/'stage_traces.json',traces);self.current.pop(worker,None)
            await model.root_async_client.close();model.root_client.close()

    def worker_key_disabled(self,worker):
        return getattr(self,'fingerprints',[None])[worker] in read(self.gate.path).get('disabled_keys',{})

    def cache_usage(self,*,current_provider_only=False):
        # Raw provider usage is the evidence for cache hits, not prompt layout alone.
        groups=collections.defaultdict(lambda:collections.Counter())
        accepted=set()
        if current_provider_only:
            for identity in self.root.glob('*/cases/*/http_identity.jsonl'):
                for line in identity.read_text().splitlines():
                    item=json.loads(line)
                    if item.get('event')=='response' and urlparse(item['url']).hostname==urlparse(self.provider['base_url']).hostname:
                        accepted.add(identity.parent/'wire'/f"{item['request_id']:06}_response.json")
        for f in self.root.glob('*/cases/*/wire/*_response.json'):
            if current_provider_only and f not in accepted:continue
            d=read(f);req=f.with_name(f.name.replace('_response','_request'));q=read(req) if req.exists() else {};messages=q.get('body',{}).get('messages',[])
            phase='guided_sql' if messages and str(messages[0].get('content','')).startswith('You are an expert data analyst.') else 'online'
            body=d.get('body') or {};usage=body.get('usage') or {} if isinstance(body,dict) else {};m=groups[phase];m['responses']+=1;m['prompt_tokens']+=usage.get('prompt_tokens',0);m['hit_tokens']+=usage.get('prompt_cache_hit_tokens',usage.get('prompt_tokens_details',{}).get('cached_tokens',0));m['miss_tokens']+=usage.get('prompt_cache_miss_tokens',0);m['http_'+str(d.get('status'))]+=1
            if 'prompt_cache_hit_tokens' in usage or 'cached_tokens' in (usage.get('prompt_tokens_details') or {}):m['cache_reported_prompt_tokens']+=usage.get('prompt_tokens',0)
            elif usage.get('prompt_tokens'):m['responses_without_cache_metrics']+=1
        result={k:dict(v) for k,v in groups.items()}
        for value in result.values():
            denom=value.get('cache_reported_prompt_tokens',0)
            value['token_hit_rate']=value['hit_tokens']/denom if denom else None
        return result

    def publish(self):
        assert sha(self.root/'seed_full_test_results.jsonl')==self.protocol['seed_sha256']
        rows=deepcopy(self.seed);blocked=[];deferred=[];new_done=0
        for r in rows:
            key=r['database_id']+'/'+str(r['source_id']);folder=self.folder(key)
            if key not in self.protocol['selected_cases']:continue
            if (folder/'case_result.json').exists():
                result=read(folder/'case_result.json');r.update(guided=result['guided'],completed=True,status='guided_completed',guided_outcome=result['outcome'],guided_result_folder=str(folder));new_done+=1
            elif (folder/'blocked.json').exists():
                result=read(folder/'blocked.json');blocked.append(result);r.update(status='online_blocked',latest_guided_attempt=result)
            elif (folder/'deferred.json').exists():
                result=read(folder/'deferred.json');deferred.append(result);r.update(status='transport_deferred',latest_guided_attempt=result)
        evaluated=[r for r in rows if r.get('completed') and r.get('guided') is not None];correct=sum(ok(r['guided']) for r in evaluated)
        bydb={}
        for db in self.graphs:
            subset=[r for r in evaluated if r['database_id']==db];bydb[db]={'total':sum(r['database_id']==db for r in rows),'guided_evaluated':len(subset),'guided_correct':sum(ok(r['guided']) for r in subset),'guided_ea':sum(ok(r['guided']) for r in subset)/len(subset) if subset else None}
        terminal=len(evaluated);status=self.phase
        if self.stop_reason:status='needs_attention_transport'
        elif self.finished:status='complete' if terminal==len(rows) else 'finished_with_blocked_cases'
        report={'checked_at':now(),'status':status,'mode':'guided_only','pid':os.getpid(),'baseline_new_requests':0,'baseline_reference':self.protocol['baseline_reference'],'baseline_records_available':self.protocol['baseline_records_available'],'guided_evaluated':terminal,'guided_correct':correct,'guided_ea':correct/terminal if terminal else None,'guided_final_ea':correct/len(rows) if terminal==len(rows) else None,'total':len(rows),'remaining':len(rows)-terminal,'new_guided_completed':new_done,'new_selected_cases':len(self.protocol['selected_cases']),'blocked':blocked,'workers':self.current,'http_admission':read(self.gate.path),'cache_usage':self.cache_usage(),'databases':bydb,'error':self.stop_reason}
        report['provider']={'base_url':self.provider['base_url'],'model':self.protocol['model'],'key_count':len(self.slots),'requested_reasoning_effort':'none','provider_reasoning_responses':report['http_admission'].get('provider_reasoning_responses',0),'worker_keys':{str(i):{'key_slot':i+1,'key_fingerprint':self.fingerprints[i]} for i in range(self.args.workers)}}
        report.update(deferred=deferred,http_policy=self.http_policy,disabled_worker_keys=report['http_admission'].get('disabled_keys',{}))
        report['current_provider_cache_usage']=self.cache_usage(current_provider_only=True)
        report['single_http_long_validation']={'required_long_successes':2,'min_prompt_tokens':16000,'stable_long_completions':report['http_admission'].get('stable_long_completions',0),'passed':report['http_admission'].get('stable_long_completions',0)>=2}
        report['workers']={str(i):{**entry,'key_slot':int(i)+1,'key_fingerprint':self.fingerprints[int(i)]} for i,entry in self.current.items()}
        usage=report['cache_usage'];tokens=sum(v.get('cache_reported_prompt_tokens',0) for v in usage.values())
        report['overall_cache_token_hit_rate']=sum(v['hit_tokens'] for v in usage.values())/tokens if tokens else None
        assert not list(self.root.glob('*/cases/*/plain_attempt_*'))
        write(self.root/'summary.json',report);write(self.root/'status.json',report);jsonl(self.root/'full_test_results.jsonl',rows)
        h='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="20"><title>BIRD Guided-only EA</title><style>body{font:15px/1.65 system-ui;margin:24px;color:#17324a;background:#f5f7fb}table{border-collapse:collapse;width:100%;background:white;margin:20px 0}td,th{border:1px solid #ccd;padding:9px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}.note{background:#fff0cf;padding:16px}</style><body><h1>BIRD剩余129条：仅运行Guided EA</h1>'
        h+=f'<p>{report["checked_at"]} · {status} · 新增完成 {new_done}/129；Guided已评测 {terminal}/244，EA {correct}/{terminal} = {report["guided_ea"]:.2%}；剩余 {report["remaining"]}。</p>'
        base=self.protocol['baseline_reference'];h+=f'<p class="note">本轮baseline调用为0。历史baseline参考 {base["correct"]}/{base["denominator"]} = {base["ea"]:.2%}，已有127条baseline记录原样保留，缺失项不补跑。Guided覆盖范围继续增长，不能直接把不同分母当成配对实验。检索用initial SQL仍是online内部输入，不计算baseline EA。</p>'
        h+='<p class="note">当前接口 '+escape(self.provider['base_url'])+'；'+str(self.args.workers)+'个worker各用独立key。HTTP从6路起，成功响应和稳定窗口满足后逐步升至'+str(self.args.max_http)+'路；服务故障并发减半，最低2路，连续8次失败冷却90秒后恢复。每个HTTP请求最多重试6次（共7次），连接/读取超时90/1800秒。401/402/403仅停用对应key、将case转交其他worker。显式none/disabled；SQL执行失败仍最多修复2次，EA不匹配不重试，服务中断不计入EA。<a href="prompt_preview.txt">SQL prompt模板</a></p>'
        h+='<table><tr><th>Database</th><th>Guided已评测/总量</th><th>Guided EA</th></tr>'
        for db,m in bydb.items():h+=f'<tr><td>{db}</td><td>{m["guided_evaluated"]}/{m["total"]}</td><td>{m["guided_correct"]}/{m["guided_evaluated"]} = {m["guided_ea"]:.2%}</td></tr>'
        rate=report['overall_cache_token_hit_rate'];rate_text=f'{rate:.2%}' if rate is not None else '暂无数据'
        h+='</table><p>真实缓存Token命中率（按输入token加权）：'+rate_text+'。各阶段比例见下方token_hit_rate。</p><pre>'+escape(json.dumps(report['cache_usage'],ensure_ascii=False,indent=2))+'</pre>'
        h+='<p>当前接口缓存统计（未返回缓存字段时标为未知，不当作0%）：</p><pre>'+escape(json.dumps(report['current_provider_cache_usage'],ensure_ascii=False,indent=2))+'</pre><p>本轮长请求稳定计数（此前单路验证已通过）：</p><pre>'+escape(json.dumps(report['single_http_long_validation'],ensure_ascii=False))+'</pre>'
        if self.provider.get('allow_provider_reasoning'):
            h+='<p class="note">请求参数保持none/disabled，但当前网关在真实请求中仍返回reasoning；额外enable_thinking=false探测同样无效。完整最终答案正常使用，reasoning单独保留审计，不作为SQL或JSON答案。已观察到此类响应 '+str(report['provider']['provider_reasoning_responses'])+' 次，不能宣称服务端实际关闭了thinking。</p>'
        recovery=self.root/'latest_recovery.json'
        if recovery.exists():h+='<p class="note">最近恢复：'+escape(read(recovery).get('description',''))+' <a href="latest_recovery.json">恢复证据</a> · <a href="latest_health_audit.json">最新核查</a></p>'
        h+='<details><summary>当前请求/worker/错误</summary><pre>'+escape(json.dumps({'workers':report['workers'],'http':report['http_admission'],'blocked':blocked,'deferred':deferred,'disabled_worker_keys':report['disabled_worker_keys'],'error':self.stop_reason},ensure_ascii=False,indent=2))+'</pre></details>'
        h+='<table><tr><th>Database</th><th>Case</th><th>状态</th><th>Guided</th><th>Baseline旧记录</th></tr>'
        for r in rows:
            pred=r.get('guided');score='通过' if ok(pred) else 'SQL错误' if pred and pred.get('error') else 'EA不匹配' if pred else '未完成'
            base='原记录通过' if ok(r.get('plain')) else '原记录未通过' if r.get('plain') else '未运行'
            h+='<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in [r['database_id'],r['source_id'],r.get('status'),score,base])+'</tr>'
        h+='</table><p><a href="full_test_results.jsonl">完整244条台账</a> · <a href="summary.json">EA与缓存统计</a> · <a href="protocol.json">运行协议</a> · <a href="runtime_manifest.json">代码快照</a></p></body></html>'
        temp=self.root/'index.html.tmp';temp.write_text(h);temp.replace(self.root/'index.html')
        pointer={'folder':str(self.root),'html':str(self.root/'index.html'),'results':str(self.root/'full_test_results.jsonl'),'summary':str(self.root/'summary.json'),'mode':'guided_only','status':status,'updated_at':now(),'guided_evaluated':terminal,'total_test_cases':len(rows),'baseline_new_requests':0}
        write(self.root.parent/'active_online_guided_only.json',pointer);write(self.root.parent/'active_online_paired.json',pointer);write(self.root.parent/'bird_online_full_results_latest.json',pointer)
        return report

    async def run(self):
        await self.prepare()
        queue=asyncio.Queue()
        # Keep neighboring requests on the same database to reuse schema prefixes.
        priority=self.protocol.get('resume_priority_cases',[])
        keys=priority+[k for k in self.protocol['selected_cases'] if k not in priority and k!='codebase_community/714']
        if 'codebase_community/714' in self.protocol['selected_cases']:keys.append('codebase_community/714')
        for key in keys:queue.put_nowait(key)
        async def worker(index):
            while not queue.empty():
                key=queue.get_nowait()
                try:await self.one(key,index)
                except WorkerKeyUnavailable as exc:
                    self.disabled_workers.add(index)
                    write(self.folder(key)/'key_reassignment.json',{'case':key,'at':now(),'worker':index,'key_slot':index+1,'reason':str(exc),'checkpoint_preserved':True})
                    queue.put_nowait(key);return
                except CaseDeferred as exc:
                    write(self.folder(key)/'deferred.json',{'case':key,'at':now(),'status':'transport_retries_exhausted','error':str(exc),'checkpoint_preserved':True,'excluded_from_ea':True})
                finally:queue.task_done()
        tasks=[]
        async def monitor():
            while not self.finished:
                self.publish();await asyncio.sleep(15)
        reporter=asyncio.create_task(monitor())
        try:
            while not queue.empty():
                available=[i for i in range(self.args.workers) if i not in self.disabled_workers]
                if not available:
                    self.stop_reason='No usable worker credentials remain; checkpoints preserved';break
                tasks=[asyncio.create_task(worker(i)) for i in available]
                await asyncio.gather(*tasks)
        except TransportStopped as exc:
            self.stop_reason=str(exc)
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        finally:
            reporter.cancel();await asyncio.gather(reporter,return_exceptions=True);self.finished=True;report=self.publish()
            preserved=self.verify_baseline_files()
            write(self.root/'baseline_preservation.json',{'checked_at':now(),'baseline_new_requests':0,'baseline_records':self.protocol['baseline_records_available'],**preserved})
            print(json.dumps({'status':report['status'],'guided_evaluated':report['guided_evaluated'],'guided_correct':report['guided_correct'],'baseline_new_requests':0}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--parent',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--workers',type=int,default=6);parser.add_argument('--max-http',type=int,default=4);parser.add_argument('--retry-blocked',action='store_true');args=parser.parse_args();load_dotenv('.env',override=False)
    args.output.mkdir(parents=True,exist_ok=True)
    with (args.output/'.runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run=GuidedRun(args)
        try:asyncio.run(run.run())
        except BaseException as exc:
            write(run.root/'fatal_error.json',classify_workflow_error(exc,stage='guided_runner'));raise


if __name__=='__main__':main()
