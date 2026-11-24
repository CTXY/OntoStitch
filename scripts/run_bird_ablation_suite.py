"""Incremental paired ablations over an immutable BIRD artifact snapshot."""
from pathlib import Path
import sys
RUNTIME=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(RUNTIME/'src'),str(RUNTIME/'baseline/src'),str(RUNTIME/'scripts')]
import argparse,asyncio,fcntl,hashlib,json,os,time,sqlite3
from collections import Counter
from html import escape
from types import SimpleNamespace
from urllib.parse import urlparse
from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from cortex.config import load_config
from cortex.bootstrap.online_context import build_online_context_service,build_simulated_user_agent
from cortex.application.online_context import ChoicePoint,InteractionAnswer,calculate_graph_state_fingerprint
from cortex.application.retrieve_db import RetrieveDbResult
from cortex.capabilities.online_context.engine import _questions
from cortex.capabilities.online_context.discovery.prioritization import rank_choice_points
from cortex.capabilities.online_context.discovery.evidence import _analysis_graph_payload
from cortex.infrastructure.online_context import FileSystemGraphStateLoader,DatabaseDescriptionLoader
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.llm.adaptive_limit import AdaptiveRequestGate,install_adaptive_gate
from cortex.infrastructure.workflow.diagnostics import classify_workflow_error
from run_merged_graph_ea import sql_arm
from run_graph_guided_test import write,read
from run_bird_dev_online_guided_baseline import _question_with_evidence

SETTINGS=('no_filter_all','retrieved_memory','priority_1','priority_2','priority_3')
def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
def policy_key(points):
 return fingerprint([{k:v for k,v in p.model_dump(mode='json').items() if k not in ['priority_score','priority_reason','source_category','applicable_candidate_count','candidate_relevance_sum','evidence_priority','evidence_priority_reason']} for p in points])
def now():return time.strftime('%Y-%m-%dT%H:%M:%S%z')
def points(raw):return tuple(ChoicePoint.model_validate(p) for p in raw)
def sorted_points(raw):return tuple(sorted(raw,key=lambda p:-p.priority_score))

class CompactAudit(BaseCallbackHandler):
 def __init__(self,folder):self.folder=folder
 def on_llm_end(self,response,**kwargs):
  usage=(response.llm_output or {}).get('token_usage') or {}
  with (self.folder/'llm_usage.jsonl').open('a') as f:f.write(json.dumps({'at':now(),'usage':usage})+'\n')
  if (self.folder/'failure.json').exists():
   outputs=[{'content':g.message.content,'tool_calls':g.message.tool_calls} for group in response.generations for g in group if hasattr(g,'message')]
   write(self.folder/'recovery_model_outputs'/f'{kwargs.get("run_id",time.time_ns())}.json',{'at':now(),'outputs':outputs,'usage':usage})

class Suite:
 def __init__(self,args):
  self.args=args;self.root=args.root.resolve();self.protocol=read(self.root/'protocol.json');self.specs=read(self.root/'cases.json')
  self.output=self.root/'experiments';self.output.mkdir(exist_ok=True)
  load_dotenv(Path(self.protocol['source_root'])/'.env',override=False)
  for k in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy']:os.environ.pop(k,None)
  os.environ['LANGSMITH_TRACING']=os.environ['LANGCHAIN_TRACING_V2']='false'
  self.cfg=load_config(RUNTIME/'configs/cortex.yaml');self.slots=read_key_slots(self.protocol['keys_file'])
  self.keys=tuple(args.key_slots);assert len(set(self.keys))==len(self.keys)
  self.graphs={};self.contexts={};self.current={};self.phase='running';self.failed=[];self.disabled=set()
  self.gate=AdaptiveRequestGate(self.output/'http_admission',max_limit=args.max_http,
   stages=tuple((6,20) for _ in range(args.max_http-1)),min_long_completions=0,initial_limit=1,min_limit=1,
   decrease_factor=.5,failure_threshold=8,cooldown_seconds=5,auto_recover_seconds=90,isolate_credentials=True,allow_provider_reasoning=True)
  for db in dict.fromkeys(s['database_id'] for s in self.specs):
   self.graphs[db]=FileSystemGraphStateLoader().load(self.root/f'databases/{db}/merged_graph.json')
   self.contexts[db]=DatabaseDescriptionLoader().load(Path(self.protocol['database_root'])/db/'database_description')

 def folder(self,s):return self.output/'cases'/s['database_id']/s['source_id']
 def state(self,s,stage,slot):self.current[str(slot)]={'case':s['case'],'stage':stage,'at':now()}
 def model(self,cfg,folder,slot):
  m=create_chat_model(cfg.llm,profile='online_context');m.callbacks=[CompactAudit(folder)]
  import httpx
  clients=[httpx.Client(timeout=httpx.Timeout(connect=90,read=1800,write=300,pool=180),trust_env=False),httpx.AsyncClient(timeout=httpx.Timeout(connect=90,read=1800,write=300,pool=180),trust_env=False)]
  m.root_client=m.root_client.with_options(http_client=clients[0],timeout=clients[0].timeout,max_retries=0);m.client=m.root_client.chat.completions
  m.root_async_client=m.root_async_client.with_options(http_client=clients[1],timeout=clients[1].timeout,max_retries=0);m.async_client=m.root_async_client.chat.completions
  keyfp=hashlib.sha256(self.slots[slot].api_key.get_secret_value().encode()).hexdigest()[:12]
  def req(r):
   body=json.loads(r.content);assert body['model']=='deepseek-v4-pro' and body.get('reasoning_effort')=='none' and body.get('thinking',{}).get('type')=='disabled'
   assert r.url.host==urlparse(cfg.llm.base_url).hostname
   assert hashlib.sha256(r.headers['authorization'].removeprefix('Bearer ').encode()).hexdigest()[:12]==keyfp
   r.extensions['started']=time.monotonic()
   with (folder/'http_audit.jsonl').open('a') as f:f.write(json.dumps({'event':'request','at':now(),'slot':slot,'key_fingerprint':keyfp,'body_sha256':hashlib.sha256(r.content).hexdigest(),'bytes':len(r.content),'reasoning_effort':body['reasoning_effort'],'thinking':body['thinking'],'timeout':r.extensions.get('timeout')})+'\n')
  def resp(r):
   with (folder/'http_audit.jsonl').open('a') as f:f.write(json.dumps({'event':'response','at':now(),'slot':slot,'status':r.status_code,'seconds':time.monotonic()-r.request.extensions.get('started',time.monotonic())})+'\n')
  async def ar(r):await r.aread();req(r)
  async def ap(r):await r.aread();resp(r)
  clients[0].event_hooks={'request':[req],'response':[resp]};clients[1].event_hooks={'request':[ar],'response':[ap]}
  install_adaptive_gate(clients[0],self.gate,max_retries=6);install_adaptive_gate(clients[1],self.gate,asynchronous=True,max_retries=6)
  return m

 def configuration(self,s,folder,slot):
  return self.cfg.model_copy(update={'llm':self.cfg.llm.model_copy(update={'api_key':self.slots[slot].api_key,'model':'deepseek-v4-pro','reasoning_effort':'none','enable_thinking':False,'temperature':0,'timeout':1800,'max_retries':0}),
   'database':self.cfg.database.model_copy(update={'path':Path(self.protocol['database_root'])/s['database_id']/(s['database_id']+'.sqlite')}),
   'services':self.cfg.services.model_copy(update={
    'online_context':self.cfg.services.online_context.model_copy(update={'checkpoint_path':folder/'online.sqlite','question_budget':None,'questions_per_round':1,'query_example_top_k':0}),
    'retrieve_db':self.cfg.services.retrieve_db.model_copy(update={'workspace':folder/'retrieval','embedding_model':None,'semantic_node_limit':30,'physical_node_limit':50})})})

 def reference(self,s):
  p=self.root/s['base']/'guided_prediction.json';return read(p) if p.exists() else None
 def save_reuse(self,folder,name,pred,source,count,reason):
  write(folder/(name+'.json'),{'prediction':pred,'origin':source,'reused':True,'question_count':count,'reason':reason,'at':now()})

 async def one(self,s,slot):
  folder=self.folder(s);folder.mkdir(parents=True,exist_ok=True)
  case=read(self.root/s['base']/'input.json');cfg=self.configuration(s,folder,slot);m=self.model(cfg,folder,slot)
  service=build_online_context_service(config=cfg,llm=m,embeddings=None)
  engine=service._engine;sim=build_simulated_user_agent(config=cfg,llm=m)
  request={'question':_question_with_evidence(case).strip(),'graph_state':self.graphs[s['database_id']].model_dump(mode='json'),'graph_state_fingerprint':calculate_graph_state_fingerprint(self.graphs[s['database_id']]),'dialect':'sqlite','initial_sql':None,'column_context':[c.model_dump(mode='json') for c in self.contexts[s['database_id']]],'question_budget':None}
  answers_file=folder/'answer_cache.json';answer_cache=read(answers_file) if answers_file.exists() else {}
  selections={};record=None;reconstructed=False
  try:
   # Fast, exact reuse is decided without invoking any online component.
   original=self.reference(s);count=s['question_count']
   if original is not None and count is not None:
    for budget in (1,2,3):
     if count<=budget and not (folder/f'priority_{budget}.json').exists():
      self.save_reuse(folder,f'priority_{budget}',original,'frozen_current',count,'All originally answered questions fit the requested budget')
   if s.get('retrieved_memory_prediction') and not (folder/'retrieved_memory.json').exists():
    self.save_reuse(folder,'retrieved_memory',read(self.root/s['retrieved_memory_prediction']),'frozen_current',0,'Existing no-guidance retrieved-memory prediction')
   if s.get('raw_context'):
    record=read(self.root/s['raw_context']);record=record['online_context'] if s.get('raw_context_nested') else record
    after=record.get('online_after_interaction') or {}
    ret=RetrieveDbResult(status='completed',question=request['question'],initial_sql=after.get('initial_sql'),graph_fingerprint=after.get('graph_state_fingerprint')or request['graph_state_fingerprint'],retrieved_graph_state=after['retrieved_graph_state'],representations=after.get('representations',[]),relevance=after.get('relevance',[]),relevant_semantic_node_ids=after.get('relevant_semantic_node_ids',[]),relevant_grounding_node_ids=after.get('relevant_grounding_node_ids',[]),selected_node_ids=after.get('selected_node_ids',[]))
   else:
    self.state(s,'reconstruct_retrieval_only',slot)
    cache=folder/'retrieval_result.json'
    if cache.exists():ret=RetrieveDbResult.model_validate(read(cache))
    else:
     ret=await engine._retrieve(request,fingerprint({'case':s['case'],'stage':'retrieve','graph':request['graph_state_fingerprint']}));write(cache,ret.model_dump(mode='json',exclude={'relevant_graph'}))
    reconstructed=True
   if ret.retrieved_graph_state is None:raise ValueError('No frozen/reconstructed memory; never label a plain baseline as retrieved-memory')
   if not (folder/'retrieved_memory.json').exists():
    self.state(s,'retrieved_memory_sql',slot)
    raw=self.root/s['retrieved_memory'] if s.get('retrieved_memory') else None
    memory=read(raw)['text'] if raw and raw.exists() else json.dumps(_analysis_graph_payload(ret.retrieved_graph_state),ensure_ascii=False,separators=(',',':'))
    pred=await asyncio.to_thread(sql_arm,case,'retrieved_memory',memory,folder/'memory_sql',m,Path(self.protocol['database_root']))
    if pred.get('error_stage')=='generation' and pred.get('error'):raise RuntimeError('Retrieved-memory SQL transport/generation interrupted')
    write(folder/'retrieved_memory.json',{'prediction':pred,'reused':False,'question_count':0,'memory_sha256':hashlib.sha256(memory.encode()).hexdigest(),'reconstructed_retrieval':reconstructed})
   pre=post=None
   if s.get('pre_filter_points') and not reconstructed:pre=points(read(self.root/s['pre_filter_points']))
   if s.get('post_filter_points') and not reconstructed:post=points(read(self.root/s['post_filter_points']))
   decisions=tuple((record or {}).get('online_after_interaction',{}).get('filter_decisions',[]))
   if pre is None:
    reconstructed=True;self.state(s,'reconstruct_discovery',slot)
    cached=folder/'discovery.json'
    if cached.exists():d=read(cached);pre=points(d['pre']);post=points(d['post']);decisions=tuple(d['decisions'])
    else:
     pre,notes=await engine._discover(request,ret)
     filt=await engine._filter(request,ret,pre);post=filt.choice_points;decisions=tuple(x.model_dump(mode='json') for x in filt.decisions)
     write(cached,{'pre':[p.model_dump(mode='json') for p in pre],'post':[p.model_dump(mode='json') for p in post],'decisions':decisions,'notes':notes})
   pre=rank_choice_points(pre);post=sorted_points(post or ())
   write(folder/'input_provenance.json',{'reconstructed_inputs':reconstructed,'reference_policy':'frozen_published','frozen_reference_exec':s['reference_exec'],'retrieval_sha256':fingerprint(ret.model_dump(mode='json',exclude={'relevant_graph'})),'pre_filter_points':[p.model_dump(mode='json') for p in pre],'post_filter_points':[p.model_dump(mode='json') for p in post]})
   # Bind reused answers to the exact historical question payload, not just its ID.
   original_points=points(read(self.root/s['post_filter_points'])) if s.get('post_filter_points') else ()
   original_by_id={p.choice_point_id:p for p in original_points}
   old_answers={a['question_id']:a for a in read(self.root/s['existing_answers'])} if s.get('existing_answers') else {}
   for p in (*post,*pre):
    q=_questions((p,))[0];qid=q.question_id;old=original_by_id.get(p.choice_point_id)
    if old is not None and policy_key((old,))==policy_key((p,)) and qid in old_answers:
     answer_cache.setdefault(fingerprint(q.model_dump(mode='json')),old_answers[qid])
   write(answers_file,answer_cache)
   async def answered(chosen):
    result=[];priorq=[]
    for p in chosen:
     q=_questions((p,))[0];qkey=fingerprint(q.model_dump(mode='json'))
     if qkey in answer_cache:a=InteractionAnswer.model_validate(answer_cache[qkey])
     else:
      self.state(s,'simulated_user_missing_answer',slot)
      a=(await sim.answer_round(question=request['question'],gold_sql=case['sql'],interaction_questions=(q,),prior_questions=tuple(priorq),prior_answers=tuple(result),dialect='sqlite'))[0]
      answer_cache[qkey]=a.model_dump(mode='json');write(answers_file,answer_cache)
     result.append(a);priorq.append(q)
    return tuple(result)
   async def variant(name,pool,chosen):
    assert name!='control','The original setting is a frozen reference, never an LLM job'
    path=folder/(name+'.json')
    if path.exists():return read(path)
    sig=fingerprint({'pool':policy_key(pool),'chosen':[p.choice_point_id for p in chosen]})
    if sig in selections:
     source=selections[sig];v=read(source);write(path,{**v,'reused':True,'origin':str(source),'same_input_signature':sig});return read(path)
    ans=await answered(chosen);stagefolder=folder/name;stagefolder.mkdir(exist_ok=True)
    if name.startswith('priority_') and len(chosen)<len(pool) and chosen:
     # Recheck the selected subset without the omitted prior questions/answers.
     # This prevents a formerly contextual answer from importing an unasked choice.
     review=stagefolder/'subset_answer_review.json'
     if review.exists():ans=tuple(InteractionAnswer.model_validate(a) for a in read(review))
     else:
      self.state(s,name+'_subset_answer_review',slot)
      ans=await sim.review_answers(question=request['question'],gold_sql=case['sql'],interaction_questions=_questions(chosen),tentative_answers=ans,dialect='sqlite')
      write(review,[a.model_dump(mode='json') for a in ans])
    self.state(s,name+'_guidance',slot)
    cache=stagefolder/'guidance.json'
    if cache.exists():g=read(cache)
    else:
     from cortex.application.online_context import ClarificationFilterDecision
     result=await engine._finish(request,ret,pool,ans,(),(),tuple(ClarificationFilterDecision.model_validate(d) for d in decisions) if name!='no_filter_all' else ())
     assert result.diagnostics.presented_questions==len(chosen)
     ids={g.choice_point_id for g in result.interaction_guidance if g.source in {'user_choice','user_explanation'}}
     assert ids=={p.choice_point_id for p in chosen},'Unasked answer entered guidance'
     g={'guidance_content':result.downstream_guidance.guidance_content,'downstream_guidance':result.downstream_guidance.model_dump(mode='json'),'presented_questions':len(chosen),'selected_question_ids':sorted(ids),'input_signature':sig};write(cache,g)
    self.state(s,name+'_sql',slot)
    pred=await asyncio.to_thread(sql_arm,case,'guided',g['guidance_content'],stagefolder,m,Path(self.protocol['database_root']))
    if pred.get('error_stage')=='generation' and pred.get('error'):raise RuntimeError(name+' SQL generation interrupted')
    value={'prediction':pred,'origin':name,'reused':False,'question_count':len(chosen),'same_input_signature':sig,'reconstructed_inputs':reconstructed,'selected_question_ids':[p.choice_point_id for p in chosen]};write(path,value);selections[sig]=path;return value
   if not (folder/'control.json').exists():self.save_reuse(folder,'control',original,'frozen_current',s['question_count'],'Fixed published reference; never regenerate the original setting')
   if not reconstructed and policy_key(pre)==policy_key(post):
    if not (folder/'no_filter_all.json').exists():self.save_reuse(folder,'no_filter_all',original,'frozen_current',s['question_count'],'No clarification candidates removed or altered by filter')
   else:await variant('no_filter_all',pre,pre)
   for k in (1,2,3):await variant(f'priority_{k}',post,post[:k])
   write(folder/'completed.json',{'at':now(),'reconstructed_inputs':reconstructed})
   failure=folder/'failure.json'
   if failure.exists():
    archived=folder/'failure_history'/f'{fingerprint(read(failure))}.json';archived.parent.mkdir(exist_ok=True)
    failure.rename(archived)
  finally:
   self.current.pop(str(slot),None);await m.root_async_client.close();m.root_client.close()

 def publish(self):
  totals={name:{'completed':0,'correct':0,'reused':0,'questions':0,'question_cases':0} for name in ('control',*SETTINGS)};complete=0;failures=[];reconstructed=0
  for s in self.specs:
   f=self.folder(s);complete+=(f/'completed.json').exists()
   if (f/'failure.json').exists():failures.append({'case':s['case'],**read(f/'failure.json')})
   if (f/'input_provenance.json').exists():
    provenance=read(f/'input_provenance.json');reconstructed+=provenance.get('reconstructed_inputs',provenance.get('reconstructed_common_input_control',False))
   for setting,row in totals.items():
    p=f/(setting+'.json')
    if not p.exists():continue
    v=read(p);pred=v.get('prediction')
    if pred is None:continue
    row['completed']+=1;row['correct']+=(pred.get('evaluation',{}).get('exec')==1);row['reused']+=v.get('reused',False)
    if v.get('question_count') is not None:row['questions']+=v['question_count'];row['question_cases']+=1
  for row in totals.values():row.update(ea_fixed_denominator=row['correct']/765,ea_observed=row['correct']/row['completed'] if row['completed'] else None,final=row['completed']==765,mean_questions=row['questions']/row['question_cases'] if row['question_cases'] else None)
  known_questions=[s['question_count'] for s in self.specs if s['question_count'] is not None]
  totals['control']={'completed':765,'correct':597,'reused':765,'questions':sum(known_questions),'question_cases':len(known_questions),'ea_fixed_denominator':597/765,'ea_observed':597/765,'final':True,'mean_questions':sum(known_questions)/len(known_questions),'policy':'Fixed original published result; zero new model calls, including originally missing predictions in the denominator.'}
  status={'at':now(),'status':self.phase,'pid':os.getpid(),'reference':{'correct':597,'total':765,'ea':597/765,'policy':'fixed_original_no_model_calls'},'settings':totals,'completed_cases':complete,'reconstructed_input_cases':reconstructed,'failures':failures,'workers':self.current,'http':read(self.gate.path)}
  write(self.output/'status.json',status)
  h='<h1>BIRD 当前版本归档与增量实验</h1><p>'+escape(now())+' · '+self.phase+'</p><p>原始setting固定597/765=78.04%，不再生成control。priority保留filter，原问题数不超过预算的case直接复用原结果。缺失历史输入的case需要重建，重建标记单独保留；不能将这些case的全部变化归因于单一setting。未完成的EA是临时下界，不能作为最终值。</p><table><tr><th>Setting</th><th>完成</th><th>正确</th><th>EA(all765)</th><th>直接复用</th><th>平均问题数(可计数)</th></tr>'
  for name,row in totals.items():h+=f'<tr><td>{"control（固定原始结果）" if name=="control" else name}</td><td>{row["completed"]}/765</td><td>{row["correct"]}</td><td>{row["ea_fixed_denominator"]:.2%}</td><td>{row["reused"]}</td><td>{row["mean_questions"]}</td></tr>'
  h+='</table><p><a href="../protocol.json">归档与实验协议</a> · <a href="../assets.json">去重产物清单</a> · <a href="status.json">实时JSON</a></p><pre>'+escape(json.dumps({'workers':self.current,'failures':failures,'reconstructed_input_cases':reconstructed},ensure_ascii=False,indent=2))+'</pre>'
  p=self.output/'index.html.tmp';p.write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="20"><title>BIRD incremental ablations</title><style>body{font:16px/1.6 system-ui;margin:28px}td,th{border:1px solid #ddd;padding:8px}table{border-collapse:collapse}pre{white-space:pre-wrap}</style>'+h);p.replace(self.output/'index.html')

 async def run(self):
  queue=asyncio.Queue()
  # Complete frozen-input cases first; legacy cases may need their experiment inputs reconstructed.
  ordered=sorted(self.specs,key=lambda s:(not bool(s.get('raw_context')),not bool(s.get('retrieved_memory_prediction')),s['case']))
  for s in ordered:
   if self.args.cases and s['case'] not in self.args.cases:continue
   if not (self.folder(s)/'completed.json').exists():queue.put_nowait(s)
  if self.args.limit:
   while queue.qsize()>self.args.limit:queue._queue.pop()
  async def worker(slot):
   while not queue.empty():
    s=await queue.get()
    try:await self.one(s,slot)
    except Exception as e:
     failure=classify_workflow_error(e);write(self.folder(s)/'failure.json',{'at':now(),**failure,'type':type(e).__name__})
     with self.gate._state() as state:disabled=state.get('disabled_keys',{})
     fp=hashlib.sha256(self.slots[slot].api_key.get_secret_value().encode()).hexdigest()[:12]
     if fp in disabled:self.disabled.add(slot);queue.put_nowait(s);return
    finally:queue.task_done();self.publish()
  async def ticker():
   while True:self.publish();await asyncio.sleep(20)
  t=asyncio.create_task(ticker())
  try:await asyncio.gather(*(worker(slot) for slot in self.keys))
  finally:
   t.cancel();self.phase='complete' if all((self.folder(s)/'completed.json').exists() for s in self.specs) else 'finished_with_pending_cases';self.publish()
  selected=[s for s in self.specs if not self.args.cases or s['case'] in self.args.cases]
  return all((self.folder(s)/'completed.json').exists() for s in selected)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--key-slots',type=int,nargs='+',default=[0,2,4,6]);p.add_argument('--max-http',type=int,default=4);p.add_argument('--limit',type=int);p.add_argument('--cases',nargs='+');a=p.parse_args()
 a.root.mkdir(exist_ok=True);lock=(a.root/'experiments.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 sys.exit(0 if asyncio.run(Suite(a).run()) else 2)
