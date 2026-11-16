"""Checkpointed test-set guidance and paired baseline evaluation over one frozen graph.

Gold SQL is available only to the existing simulated user and SQL evaluator.
No per-case success retries, graph writes, or answer selection are performed.
"""
from __future__ import annotations
import argparse, asyncio, dataclasses, hashlib, json, os, sqlite3, threading, time
from pathlib import Path
from types import SimpleNamespace
import yaml
from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from cortex.bootstrap.online_context import build_online_context_service, build_simulated_user_agent
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.online_context import DatabaseDescriptionLoader, FileSystemGraphStateLoader
from run_bird_dev_online_guided_baseline import _online_case, _guidance_case


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str));tmp.replace(path)


def read(path): return json.loads(path.read_text())


class CallAudit(BaseCallbackHandler):
    def __init__(self, folder):
        self.folder=folder;self.lock=threading.Lock();self.starts={}
    def append(self, data):
        with self.lock:
            with (self.folder/'calls.jsonl').open('a') as f:f.write(json.dumps(data,ensure_ascii=False,default=str)+'\n')
    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.starts[str(run_id)]=time.time()
        self.append({'event':'start','id':str(run_id),'time':time.time(),'messages':[[m.model_dump(mode='json') for m in batch] for batch in messages]})
    def on_llm_end(self, response, *, run_id, **kwargs):
        usage=(response.llm_output or {}).get('token_usage') or {}
        self.append({'event':'end','id':str(run_id),'seconds':time.time()-self.starts.get(str(run_id),time.time()),'usage':usage,'messages':[g.message.model_dump(mode='json') for group in response.generations for g in group if hasattr(g,'message')]})
    def on_llm_error(self, error, *, run_id, **kwargs):
        self.append({'event':'error','id':str(run_id),'type':type(error).__name__,'time':time.time()})


def audit_http(model, folder):
    """Count actual SDK requests as well as logical LangChain calls; no headers."""
    # LangChain may cache an HTTP client across models sharing a base URL.
    # Give this case its own transports before installing per-case audit hooks.
    sync=type(model.root_client._client)(timeout=3600)
    asynchronous=type(model.root_async_client._client)(timeout=3600)
    model.root_client=model.root_client.with_options(http_client=sync)
    model.client=model.root_client.chat.completions
    model.root_async_client=model.root_async_client.with_options(http_client=asynchronous)
    model.async_client=model.root_async_client.chat.completions
    folder.mkdir(parents=True,exist_ok=True);lock=threading.Lock();offset=max((int(x.name.split('_')[0]) for x in folder.glob('*_request.json')),default=0);counter=[offset];starts={}
    def request(req):
        with lock:counter[0]+=1;key=counter[0];starts[key]=time.monotonic()
        req.extensions['local_audit_id']=key
        write(folder/f'{key:06}_request.json',{'time':time.time(),'body':json.loads(req.content)})
    def response(resp):
        key=resp.request.extensions.get('local_audit_id',0)
        write(folder/f'{key:06}_response.json',{'status':resp.status_code,'seconds':time.monotonic()-starts.get(key,time.monotonic()),'body':resp.json()})
    def sync_response(resp):resp.read();response(resp)
    async def async_request(req):await req.aread();request(req)
    async def async_response(resp):await resp.aread();response(resp)
    model.root_client._client.event_hooks={'request':[request],'response':[sync_response]}
    model.root_async_client._client.event_hooks={'request':[async_request],'response':[async_response]}


def is_transport_failure(record):
    detail=json.dumps(record.get('error_chain',[]))+' '+str(record.get('error') or '')
    return any(x in detail for x in ('Connection','Timeout','RateLimit','APIError','LLMInvocationError','502','503','504','429','Broken pipe'))


def baseline(case, guidance, mode, folder, model, args):
    from nl2sql_baseline.config import load_settings
    from nl2sql_baseline.generation import SqlGenerator
    from nl2sql_baseline.workflow import Workflow
    saved=folder/f'{mode}_prediction.json'
    if saved.exists():return read(saved)
    path=folder/'baseline_cases.json';write(path,[case])
    cfg={'dataset':{'type':'bird'},'test_set_path':str(path.absolute()),'workspace_root':str((folder/mode).absolute()),
         'database':{'root':str(args.database_root.absolute())},'schema_linking':{'type':'full_schema'},
         'memory':{'type':'downstream_guidance' if mode=='guided' else 'none','source_path':str((folder/'guidance').absolute())},
         'llm':{'model':args.model,'base_url':'${CORTEX_LLM_BASE_URL}','api_key':'${CORTEX_LLM_API_KEY}','temperature':0,'timeout':3600},
         'evaluation':{'top_k':5,'sql_timeout':30}}
    config=folder/f'{mode}.yaml';config.write_text(yaml.safe_dump(cfg,sort_keys=False))
    generator=SqlGenerator(model=args.model,api_key='injected-model',base_url=None,temperature=0,timeout=3600,llm=model)
    workflow=Workflow(load_settings(config,env_file=Path('.env')),generator=generator)
    # Only transport failures may retry. Incorrect SQL is retained, never resampled.
    for attempt in range(3):
        next_attempt=len(list(folder.glob(f'{mode}_attempt_*')))+1
        run, report=workflow.evaluate(output=folder/f'{mode}_attempt_{next_attempt}')
        prediction=json.loads((run/'predictions.jsonl').read_text().splitlines()[0])
        if prediction.get('error') and is_transport_failure(prediction) and attempt<2:
            time.sleep(15);continue
        write(saved,prediction);return prediction


async def evaluate(args):
    load_dotenv('.env',override=False)
    os.environ['LANGCHAIN_TRACING_V2']='false';os.environ['LANGSMITH_TRACING']='false'
    args.output=args.output.absolute();args.output.mkdir(parents=True,exist_ok=True)
    cases=[json.loads(x) for x in args.cases.read_text().splitlines() if x.strip()]
    if args.limit:cases=cases[:args.limit]
    if len({str(x['source_id']) for x in cases})!=len(cases):raise ValueError('Duplicate case ID')
    cfg=load_config(args.config);slots=read_key_slots(args.keys)
    selected=getattr(args, 'key_slots', None)
    if selected is not None:
        if not selected or len(set(selected)) != len(selected) or any(i < 0 or i >= len(slots) for i in selected):
            raise ValueError('key_slots must name distinct configured credential indices')
        slots=tuple(slots[i] for i in selected)
    graph=FileSystemGraphStateLoader().load(args.graph_state)
    context=DatabaseDescriptionLoader().load(args.database_root/cases[0]['database_id']/'database_description')
    sem=asyncio.Semaphore(min(args.workers,len(slots)));records={}
    async def one(index,case):
        async with sem:
            folder=args.output/'cases'/str(case['source_id']);folder.mkdir(parents=True,exist_ok=True)
            slot=slots[index%len(slots)]
            casecfg=cfg.model_copy(update={'llm':cfg.llm.model_copy(update={'model':args.model,'api_key':slot.api_key,'timeout':3600,'max_retries':8,'enable_thinking':False}),
                'database':cfg.database.model_copy(update={'path':args.database_root/case['database_id']/f"{case['database_id']}.sqlite"}),
                'services':cfg.services.model_copy(update={
                    'online_context':cfg.services.online_context.model_copy(update={'checkpoint_path':folder/'online.sqlite','query_example_top_k':0}),
                    'retrieve_db':cfg.services.retrieve_db.model_copy(update={'workspace':folder/'retrieval','embedding_model':None})})})
            model=create_chat_model(casecfg.llm,profile='online_context');model.callbacks=[CallAudit(folder)];audit_http(model,folder/'wire')
            traces=[];worker=SimpleNamespace(service=build_online_context_service(config=casecfg,llm=model,embeddings=None,trace_sink=lambda stage,payload: traces.append({"stage":stage,"payload":payload})),simulator=build_simulated_user_agent(config=casecfg,llm=model),traces=traces)
            if getattr(args, 'replay_discovery', None):
                # Controlled comparison only: unchanged pre-Filter inputs, never
                # prior choices, SQL correctness, or targeted repair feedback.
                from langgraph.checkpoint.sqlite import SqliteSaver
                from cortex.application.retrieve_db import RetrieveDbResult
                from cortex.application.online_context import ChoicePoint
                original = args.replay_discovery/'cases'/str(case['source_id'])
                with sqlite3.connect(original/'online.sqlite') as connection:
                    thread_id=connection.execute('SELECT DISTINCT thread_id FROM checkpoints').fetchone()[0]
                with SqliteSaver.from_conn_string(str(original/'online.sqlite')) as saver:
                    snapshot=saver.get_tuple({'configurable':{'thread_id':thread_id}})
                frozen=RetrieveDbResult.model_validate(snapshot.checkpoint['channel_values']['retrieval'])
                packets=[read(p)['body'] for p in sorted((original/'wire').glob('*_request.json'))]
                packet=next(b for b in packets if any('Filter proposed clarification questions' in str(m.get('content','')) for m in b.get('messages',()) if m.get('role')=='system'))
                source=json.loads(next(m['content'] for m in packet['messages'] if m['role']=='user'))
                fixed_points=tuple(ChoicePoint.model_validate(p) for p in source['slots'])
                async def replay_retrieve(request, run_id): return frozen
                async def replay_discover(request, retrieval): return fixed_points, ()
                worker.service._engine._retrieve=replay_retrieve
                worker.service._engine._discover=replay_discover
                write(folder/'replayed_discovery.json',{'source':str(original),'retrieval':frozen.model_dump(mode='json',exclude={'relevant_graph'}),'points':[p.model_dump(mode='json') for p in fixed_points]})
            run_id=hashlib.sha256(str(case['source_id']).encode()).hexdigest()
            if (folder/'online.sqlite').exists():
                with sqlite3.connect(folder/'online.sqlite') as connection:
                    previous=connection.execute('SELECT DISTINCT thread_id FROM checkpoints').fetchall()
                    if len(previous)==1:run_id=previous[0][0]
            p=folder/'online.json'
            record=read(p) if p.exists() else None
            if not record or record.get('status')=='error':
                for attempt in range(3):
                    record=await _online_case(worker,graph_state=graph,column_context=context,case=case,run_id=run_id)
                    next_attempt=len(list(folder.glob('online_attempt_*.json')))+1
                    write(folder/f'online_attempt_{next_attempt}.json',record)
                    if record.get('status')!='error' or not is_transport_failure(record):break
                    await asyncio.sleep(15)
                write(p,record)
            item=_guidance_case(case,record);write(folder/'guidance'/f"{case['source_id']}.json",item)
            if not record.get('guidance_content'):
                records[str(case['source_id'])]={'online':record.get('status'),'error':record.get('error')}
            else:
                guided=await asyncio.to_thread(baseline,case,item,'guided',folder,model,args)
                plain=await asyncio.to_thread(baseline,case,item,'plain',folder,model,args) if args.control else None
                records[str(case['source_id'])]={'online':record['status'],'guided':guided,'plain':plain}
            await model.root_async_client.close();model.root_client.close()
            write(args.output/'progress.json',{'total':len(cases),'completed':len(records),'results':records,'updated':time.time()})
            print(json.dumps({'source_id':case['source_id'],'completed':len(records),'online':record.get('status'),'exec':records[str(case['source_id'])].get('guided',{}).get('evaluation',{}).get('exec')},ensure_ascii=False),flush=True)
    await asyncio.gather(*(one(i,c) for i,c in enumerate(cases)))
    guidance=[];predictions=[];plain=[];online=[]
    for case in cases:
        folder=args.output/'cases'/str(case['source_id']);online.append(read(folder/'online.json'));guidance.append(read(folder/'guidance'/f"{case['source_id']}.json"))
        if (folder/'guided_prediction.json').exists():predictions.append(read(folder/'guided_prediction.json'))
        if (folder/'plain_prediction.json').exists():plain.append(read(folder/'plain_prediction.json'))
    write(args.output/'guidance_cases.json',{'cases':guidance});write(args.output/'online_results.json',{'results':online})
    for name,rows in [('predictions',predictions),('plain_predictions',plain)]:
        (args.output/f'{name}.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows))
    summary={'total':len(cases),'guidance_cases':sum(bool(x.get('guidance_content')) for x in online),'guided_correct':sum(x['evaluation']['exec']==1 for x in predictions),'plain_correct':sum(x['evaluation']['exec']==1 for x in plain),'predictions':len(predictions),'control_predictions':len(plain),'evaluator':'complete result-row set equality; all test cases remain denominator','protocol':'Gold SQL only to simulated user after system-generated questions exist, and SQL evaluator. No per-case selection or correctness retries.'}
    summary['guided_ea']=summary['guided_correct']/len(cases);summary['plain_ea']=summary['plain_correct']/len(cases) if args.control else None
    write(args.output/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for key in ['cases','graph-state','config','database-root','keys','output']:p.add_argument('--'+key,type=Path,required=True)
    p.add_argument('--key-slots',type=int,nargs='+',default=None,help='Use only these credential indices, leaving other background tasks on their assigned pools')
    p.add_argument('--replay-discovery',type=Path,help='Controlled Filter-downstream comparison using an existing discovery snapshot; not a from-zero run');p.add_argument('--model',default='deepseek-v4-pro');p.add_argument('--workers',type=int,default=8);p.add_argument('--limit',type=int);p.add_argument('--control',action='store_true')
    asyncio.run(evaluate(p.parse_args()))
if __name__=='__main__':main()
