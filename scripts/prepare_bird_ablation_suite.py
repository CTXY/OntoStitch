"""Deduplicated, self-contained result snapshot and incremental ablation inputs."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'baseline/src')]
import argparse,ctypes,hashlib,json,os,sqlite3
from collections import Counter
from functools import lru_cache
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

OUT=ROOT/'output'
def read(p):return json.loads(Path(p).read_text())
def rows(p):return [json.loads(l) for l in Path(p).read_text().splitlines() if l.strip()]
def digest(b):return hashlib.sha256(b).hexdigest()
def write(p,d):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')

class Store:
 def __init__(self,root):self.root=root;self.methods=Counter();self.assets={};self.lib=ctypes.CDLL(None,use_errno=True)
 def add(self,relative,source=None,data=None,text=None):
  b=Path(source).read_bytes() if source else text.encode() if text is not None else (json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode()
  h=digest(b);blob=self.root/'objects'/h[:2]/h
  if not blob.exists():
   blob.parent.mkdir(parents=True,exist_ok=True)
   if source and hasattr(self.lib,'clonefile') and self.lib.clonefile(os.fsencode(Path(source).resolve()),os.fsencode(blob),0)==0:self.methods['apfs_copy_on_write_clone']+=1
   else:blob.write_bytes(b);self.methods['new_unique_content']+=1
   assert digest(blob.read_bytes())==h
  else:self.methods['deduplicated_reference']+=1
  dest=self.root/relative;dest.parent.mkdir(parents=True,exist_ok=True)
  if not dest.exists():dest.symlink_to(os.path.relpath(blob,dest.parent))
  self.assets[str(relative)]={'sha256':h,'bytes':len(b),'source':str(Path(source).resolve()) if source else 'derived_from_frozen_artifacts'}
  return str(relative)

@lru_cache(maxsize=3)
def document(path):
 p=Path(path)
 if p.suffix=='.jsonl':return {str(x.get('source_id',x.get('question_id'))):x for x in rows(p)}
 return read(p)

def checkpoint_points(folder,run_id,question):
 for p in sorted(folder.glob('online*.sqlite')):
  try:
   with sqlite3.connect(f'file:{p.resolve()}?mode=ro',uri=True) as c:
    threads=[x[0] for x in c.execute("select distinct thread_id from checkpoints where checkpoint_ns=''")]
    if run_id in threads:threads=[run_id]
    for thread in threads:
     rr=c.execute("select type,checkpoint from checkpoints where checkpoint_ns='' and thread_id=? order by checkpoint_id",(thread,))
     for t,b in rr:
      v=JsonPlusSerializer().loads_typed((t,b))['channel_values']
      if 'branch:to:filter_questions' in v and v.get('request',{}).get('question','').split('\nEvidence:')[0]==question:
       return v.get('points',[]),str(p)
  except sqlite3.Error:continue
 return None,None

def prepare(target):
 target.mkdir(parents=True,exist_ok=True);store=Store(target)
 old={ (r['database_id'],r['source_id']):r for r in rows(OUT/'bird_unified_nine_20261120_1700/test_results_processed.jsonl')}
 stats=rows(OUT/'bird_guided_statistics_20261124/cases.jsonl')
 dev={(r['db_id'],str(r['question_id'])):r for r in read(ROOT/'datasets/bird/dev/dev.json')}
 graphmeta=read(OUT/'latest_graphs/manifest.json')['databases']
 extra={'thrombosis_prediction':OUT/'online_json_fix_20261123_195329','toxicology':OUT/'toxicology_guided_recovery_20261124_001002'}
 extra_rows={db:{str(r['source_id']):r for r in rows(d/'predictions.jsonl')} for db,d in extra.items()}
 dbs=list(dict.fromkeys(r['database_id'] for r in stats));coverage=Counter();cases=[]
 for db in dbs:
  gm=graphmeta[db]
  for sid,meta in gm['cases'].items():
   p=OUT/'latest_graphs'/db/'case_level_graphs'/sid/'state.json';assert digest(p.read_bytes())==meta['sha256']
   store.add(Path('databases')/db/'offline/case_level_graphs'/f'{sid}.json',source=p)
  if db in extra:
   parent=Path(read(extra[db]/'source.json')['parent']);graph=parent/'merged_state.json'
  else:graph=OUT/'bird_unified_nine_20261120_1700'/db/'merged/graph.json'
  store.add(Path('databases')/db/'merged_graph.json',source=graph)
  store.add(Path('databases')/db/'offline/provenance.json',data=gm)
  training=OUT/'latest_results/training'/db/'sql_records.jsonl'
  if training.exists():store.add(Path('databases')/db/'offline/sql_records.jsonl',source=training)
 for count,s in enumerate(stats):
  db,sid=s['database_id'],s['source_id'];k=(db,sid);base=Path('databases')/db/'test'/sid;v=dev[k]
  case={'source_id':sid,'database_id':db,'db_id':db,'question':v['question'],'evidence':v.get('evidence',''),'sql':v['SQL']}
  store.add(base/'input.json',data=case)
  store.add(base/'gold.sql',text=case['sql']+'\n')
  if k in old:
   record=old[k];pred=record['guided'];guidance=record.get('guidance');plain=record.get('baseline');memory=None
  else:
   record=extra_rows[db][sid];pred=record['guided'];plain=record.get('plain');memory=record.get('retrieved_memory');guidance=None
  for arm,p in [('guided',pred),('plain',plain),('retrieved_memory',memory)]:
   if p is not None:
    store.add(base/f'{arm}_prediction.json',data=p)
    if p.get('predicted_sql'):store.add(base/f'{arm}.sql',text=p['predicted_sql'].rstrip()+'\n')
  info={'case':db+'/'+sid,'database_id':db,'source_id':sid,'base':str(base),'reference_exec':s['guided_exec'],'reference_error':s['guided_error'],'graph':f'databases/{db}/merged_graph.json','raw_context':None,'pre_filter_points':None,'post_filter_points':None,'existing_answers':None,'retrieved_memory':None,'retrieved_memory_prediction':str(base/'retrieved_memory_prediction.json') if memory else None,'question_count':s['questions_resolved']}
  src=Path(s['online_source']) if s.get('online_source') else None
  online=None;folder=None
  if src and src.exists():
   d=document(str(src))
   if src.suffix=='.jsonl':d=d.get(sid,{})
   online=d.get('online_context') or (d if 'online_after_interaction' in d else None)
   if online:
    store.add(base/'online_source.json',source=src)
    # JSONL exports have no online_context; native JSON can be read directly by the runner.
    info['raw_context']=str(base/'online_source.json');info['raw_context_nested']='online_context' in d
    folder=src.parent.parent if src.parent.name=='guidance' else src.parent
  if online:
   after=online.get('online_after_interaction') or {};before=online.get('online_before_interaction')or{}
   post=after.get('choice_points',before.get('choice_points'));raw=None;rawsource=None
   decisions=after.get('filter_decisions',[])
   if post is not None and {d['choice_point_id'] for d in decisions}<={p['choice_point_id'] for p in post}:
    raw=post;rawsource='all_filter_decisions_retained_or_no_candidates'
   else:raw,rawsource=checkpoint_points(folder,after.get('run_id'),case['question'])
   if raw is not None:info['pre_filter_points']=store.add(base/'pre_filter_points.json',data=raw);info['pre_filter_source']=rawsource
   if post is not None:info['post_filter_points']=store.add(base/'post_filter_points.json',data=post)
   answers={a['question_id']:a for a in online.get('simulated_user_decisions',[])}
   # The selected source may itself be a resumed run. Persisted answers in its
   # checkpoint take precedence over reconstructing answers from prose.
   for p in sorted(folder.glob('online*.sqlite')):
    try:
     from cortex.capabilities.online_context.workflow.migration import checkpoint_values
     with sqlite3.connect(f'file:{p.resolve()}?mode=ro',uri=True) as c:threads=[x[0] for x in c.execute("select distinct thread_id from checkpoints where checkpoint_ns=''")]
     for thread in threads:
      values=checkpoint_values(p,thread)
      if values.get('request',{}).get('question','').split('\nEvidence:')[0]!=case['question']:continue
      # Only answers belonging to this final retained set of choice points.
      existing={p['choice_point_id'] for p in post or []}
      if {p['choice_point_id'] for p in values.get('points',[])}==existing and values.get('points',[])==post:
       for a in values.get('answers',[]):answers.setdefault(a['question_id'],a)
    except sqlite3.Error:continue
   info['existing_answers']=store.add(base/'answers.json',data=list(answers.values()))
   if after.get('retrieved_graph_state') is not None:coverage['frozen_retrieval']+=1
   else:info['raw_context']=None
   for name in ['retrieved_memory.json','retrieval_result.json']:
    f=folder/name
    if f.exists():
     rel=store.add(base/name,source=f)
     if name=='retrieved_memory.json':info['retrieved_memory']=rel
   if guidance is None:guidance={'source_id':sid,'database_id':db,'guidance_content':online.get('guidance_content',''),'downstream_guidance':online.get('downstream_guidance') or after.get('downstream_guidance')}
  if guidance is not None:store.add(base/'guidance.json',data=guidance)
  if info['pre_filter_points'] is not None:coverage['pre_filter_candidates']+=1
  if info['post_filter_points'] is not None:coverage['post_filter_candidates']+=1
  if info['raw_context'] is None:coverage['requires_context_reconstruction']+=1
  if memory:coverage['retrieved_memory_predictions_reused']+=1
  cases.append(info)
  if count%75==0:print('archived',count,'/',len(stats),flush=True)
 assert len(cases)==765
 write(target/'cases.json',cases);write(target/'assets.json',store.assets)
 protocol={'snapshot':'BIRD current retained results 2026-11-24','source_root':str(ROOT),'database_root':'/Users/yichendezaizai/CodeBase/Text2SQL/datasets/bird/dev/dev_databases','keys_file':str(ROOT/'configs/sql_training.keys'),'config':str(ROOT/'configs/cortex.yaml'),'settings':['no_filter_all','retrieved_memory','priority_1','priority_2','priority_3'],'priority_filter':'enabled','model':'deepseek-v4-pro','reference_correct':597,'total':765,'coverage':dict(coverage),'storage':dict(store.methods),'protocol':'Current guided outcomes are frozen. No EA mismatch resampling. Reuse unchanged inputs and outcomes. Missing historical context requires a reconstructed common-input control; never label unknown as unchanged. Priority caps count user-facing questions after filter and exclude unasked answers from synthesis and projection.'}
 write(target/'protocol.json',protocol)
 print(json.dumps(protocol,ensure_ascii=False),flush=True)

if __name__=='__main__':
 a=argparse.ArgumentParser();a.add_argument('--output',type=Path,required=True);args=a.parse_args();prepare(args.output.resolve())
