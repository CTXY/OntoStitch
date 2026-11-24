"""Remove verified redundant archived SSE transport copies, never active runs.

Requests, complete responses, incomplete/error streams, graphs, checkpoints,
source evidence, prompts, traces and reports are retained. Planning and applying
are separate; immutable files are rechecked immediately before deletion.
"""
import argparse
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil

from cortex.infrastructure.llm.stream_audit import completion_from_sse,validate_stream_completion

REPO=Path(__file__).resolve().parents[1]
MARKERS=('thrombosis','toxicology','node_multi','node_partition','merge_resume','signal_subgraphs','signal_receipts','thinking_disabled')
def digest(p):return sha256(p.read_bytes()).hexdigest()
def roots_and_active():
 active=json.loads((REPO/'output/active_merge_runs.json').read_text())
 protected=[Path(row['root']).resolve() for row in active['databases'].values()]
 roots=[p.resolve() for p in (REPO/'output').iterdir() if p.is_dir() and not p.is_symlink() and any(m in p.name for m in MARKERS)]
 return roots,protected
def protected_path(path,protected):return any(path==root or path.is_relative_to(root) for root in protected)
def plan(report):
 roots,protected=roots_and_active();deletions=[];skipped={};verified={}
 def keep(reason):skipped[reason]=skipped.get(reason,0)+1
 for root in sorted(roots):
  if protected_path(root,protected):keep('active_run_directory');continue
  for directory,dirs,names in os.walk(root,followlinks=False):
   dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink()]
   for name in sorted(names):
    file=Path(directory)/name
    if file.is_symlink():continue
    if '__pycache__' in file.parts or '.pytest_cache' in file.parts:
     deletions.append({'path':str(file),'kind':'cache','bytes':file.stat().st_size,'sha256':digest(file)})
     continue
    if not name.endswith('_stream.sse'):continue
    prefix=name.removesuffix('_stream.sse');response=file.with_name(prefix+'_response.json');progress=file.with_name(prefix+'_progress.json')
    if not response.is_file():keep('no_complete_response');continue
    try:
     data=json.loads(response.read_text());body=data.get('body')
     if data.get('status')!=200 or not isinstance(body,dict):keep('http_error_or_nonchat');continue
     if progress.exists():
      p=json.loads(progress.read_text())
      if not p.get('completed') or p.get('model_complete') is False:keep('incomplete_stream');continue
     actual=completion_from_sse(file.read_text());validate_stream_completion(actual)
     if actual!=body:keep('response_not_exactly_equal');continue
     choices=body.get('choices',[])
     if any(c.get('finish_reason') not in ('stop','tool_calls') for c in choices):keep('non_success_terminal_reason');continue
     for c in choices:
      for call in c.get('message',{}).get('tool_calls',[]):json.loads(call['function']['arguments'])
    except Exception:
     keep('parse_error_or_invalid_tool_arguments');continue
    if str(response) not in verified:verified[str(response)]={'sha256':digest(response),'bytes':response.stat().st_size}
    deletions.append({'path':str(file),'kind':'redundant_completed_sse','bytes':file.stat().st_size,'sha256':digest(file),
                      'retained_response':str(response),'response_sha256':verified[str(response)]['sha256'],
                      'verification':'completion_from_sse equals retained response.body exactly; successful terminal model result'})
 result={'created_at':datetime.now(timezone.utc).isoformat(),'mode':'plan','protected_active_roots':[str(p) for p in protected],
         'scope_roots':[str(p) for p in roots],'deletions':deletions,'count':len(deletions),
         'logical_bytes':sum(x['bytes'] for x in deletions),'skipped':skipped,
         'retained_response_manifest':verified,'protected_types':['graphs','checkpoints','requests','responses','source evidence','failed streams','incomplete streams','model traces','reports','source snapshots']}
 report.mkdir(parents=True,exist_ok=True);(report/'cleanup_plan.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in result.items() if k in ('count','logical_bytes','skipped','protected_active_roots')},ensure_ascii=False),flush=True)
def apply(report):
 p=json.loads((report/'cleanup_plan.json').read_text());_,active=roots_and_active();done=[];skip=[]
 for row in p['deletions']:
  file=Path(row['path'])
  if protected_path(file,active) or file.is_symlink() or not file.is_relative_to(REPO/'output'):raise ValueError('Deletion outside approved archived scope')
  if not file.exists() or digest(file)!=row['sha256']:skip.append({'path':str(file),'reason':'missing_or_changed_after_plan'});continue
  if row['kind']=='redundant_completed_sse':
   response=Path(row['retained_response'])
   if not response.exists() or digest(response)!=row['response_sha256']:raise ValueError('Retained response changed; abort')
  file.unlink();done.append(row)
 # Only remove now-empty cache directories; no source/report folders are removed.
 for root in map(Path,p['scope_roots']):
  if protected_path(root,active):continue
  directories=sorted((d for d in root.rglob('*') if d.is_dir() and not d.is_symlink()
                      and ('__pycache__' in d.parts or '.pytest_cache' in d.parts)),key=lambda d:len(d.parts),reverse=True)
  for d in directories:
   try:d.rmdir()
   except OSError:pass
 for name,row in p['retained_response_manifest'].items():assert digest(Path(name))==row['sha256']
 result={'completed_at':datetime.now(timezone.utc).isoformat(),'deleted_files':len(done),'deleted_logical_bytes':sum(x['bytes'] for x in done),
         'deleted_sse':sum(x['kind']=='redundant_completed_sse' for x in done),'deleted_cache':sum(x['kind']=='cache' for x in done),
         'protected_active_roots':[str(x) for x in active],'retained_responses_verified':len(p['retained_response_manifest']),
         'skipped_after_plan':skip,'deleted':done}
 (report/'cleanup_result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in result.items() if k!='deleted'},ensure_ascii=False),flush=True)
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['plan','apply']);parser.add_argument('--report',type=Path,required=True);a=parser.parse_args()
 (plan if a.mode=='plan' else apply)(a.report.resolve())
