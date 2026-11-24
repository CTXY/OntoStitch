"""Audited rollout of node multi-results; resume saved reconciliation Assessment.

Export premises with the frozen source runtime first. Prepare independently
cloned runs, retain the exact pending draft/candidate, and attach only proven
unchanged per-signal accepts. No accepted history or original files are edited.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.runtime.state import ContractVersions,TaskState,build_contract_versions,canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.reconciliation.premises import signal_premise
from cortex.capabilities.graph_merge.reconciliation.receipts import dependency_facts,policy_hash
from cortex.capabilities.graph_merge.reconciliation.scope_migration import build_scope_migration,validate_scope_migrations
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository,SqlTrainingGraphMergeEvidenceResolver
from cortex.schema import GraphState,load_root_schema

REPO=Path(__file__).resolve().parents[1]
def read(p):return json.loads(p.read_text())
def write(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def checkpoint(root):
 plan=read(root/'plan.json');folders=sorted(p for p in (root/'merge').glob('batch-*') if (p/'workspace.sqlite').exists() and not (p/'completed.json').exists())
 if len(folders)!=1:raise ValueError('Require one unfinished batch')
 folder=folders[0];ident=f'continue-{plan["database"]}-{folder.name}'
 return plan,folder,ident,WorkspaceStore(folder/'workspace.sqlite').load(ident)
def setup(root,folder,payload,versions):
 before=GraphState.model_validate(read(folder/'before.json'));incoming=GraphState.model_validate(read(folder/'incoming.json'));schema=load_root_schema()
 raw=IntegrationContext(before,incoming,schema)
 sources=tuple(dict.fromkeys(s for obj in (*raw.raw_nodes.values(),*raw.raw_edges.values()) for s in
   (*obj.sources,*getattr(obj,'common_sources',()),*(s for a in getattr(obj,'alternatives',()) for s in a.sources))))
 resolver=SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(read(root/'source_evidence.json')))
 ctx=IntegrationContext(before,incoming,schema,evidence=resolver.resolve(sources));ctx.sql_dialect='sqlite'
 from cortex.capabilities.graph_merge.engine import input_fingerprint
 assert input_fingerprint(ctx)==payload['input_fingerprint']
 return SimpleNamespace(ctx=ctx,current=GraphState.model_validate(payload['current']),signal_receipts=payload['signal_receipts'],
                        signal_scope_migrations=payload.get('signal_scope_migrations',[]),contract_versions=versions)
def export(root,out):
 plan,folder,ident,p=checkpoint(root);versions=build_contract_versions(load_root_schema());assert versions.to_dict()==p['contract_versions'],'Exporter must run under the exact frozen source contract'
 engine=setup(root,folder,p,versions);rows=[]
 for r in p['signal_receipts']:
  facts={'check':signal_premise(engine.ctx,engine.current,r['check']),'supplementary':dependency_facts(engine.ctx,engine.current,r['dependencies'])}
  rows.append({'receipt_record_hash':r['record_hash'],'current_hash':canonical_hash(engine.current),'policy_hash':r['policy_hash'],
    'source_contract_versions':p['contract_versions'],'premise':facts,'still_matches':canonical_hash(facts)==r['premise_hash']})
 result={'run_id':ident,'source_contract_versions':p['contract_versions'],'source_current_hash':canonical_hash(engine.current),'records':rows}
 write(out,result);print(json.dumps({'export':str(out),'receipts':len(rows),'still_matching':sum(r['still_matches'] for r in rows)}),flush=True)
def prepare(source,destination,audit_file,frozen_source,report):
 from migrate_reviewed_merge_continuation import migrate_reviewed_continuation
 from cortex.capabilities.graph_merge.llm.recovery import RecoveryTracker
 plan,folder,ident,original=checkpoint(source);audit=read(audit_file)
 assert original['contract_versions']==audit['source_contract_versions']
 old=original['task_state'];assert old['stage']=='assessing' and old['phase']=='reconcile'
 assert old['review'] is None and old['candidate_draft_id']==old['draft_id']
 assert old['base_hash']==canonical_hash(original['current'])
 assert old['candidate_hash']==canonical_hash(old['last_valid_candidate'])
 versions=build_contract_versions(load_root_schema());transition_files={}
 for p in (REPO/'src/cortex/capabilities/graph_merge').rglob('*'):
  if not p.is_file() or p.suffix not in ('.py','.md','.json'):continue
  rel=p.relative_to(REPO);before=frozen_source/'cortex'/p.relative_to(REPO/'src/cortex')
  if before.exists() and before.read_bytes()!=p.read_bytes():
   transition_files[str(rel)]={'before':sha256(before.read_bytes()).hexdigest(),'after':sha256(p.read_bytes()).hexdigest()}
 assert transition_files
 # Semantic assessment requirements and graph schema are unchanged. Changes
 # concern node partitioning, feedback serialization and bounded read recovery.
 old_policy=canonical_hash({'receipt_protocol':1,'contracts':original['contract_versions']})
 transition={'source_policy_hash':old_policy,'target_policy_hash':canonical_hash({'receipt_protocol':1,'contracts':versions.to_dict()}),'files':transition_files}
 migration=migrate_reviewed_continuation(source,destination,versions=versions)
 _,target,_,p=checkpoint(destination)
 # Retain the exact head and evidence ledger; restart only the failed evaluator's
 # retry window. The original tracker and full response stay in migration_source.
 task=deepcopy(old);task['versions']=versions.to_dict();task['compilation_binding']=None
 reset=[]
 for key,record in task['agent_recovery'].items():
  if record.get('scope',{}).get('phase')!='graph_assess':continue
  reset.append({'key':key,'old_tracker':deepcopy(record['tracker']),'old_requests':record['requests']})
  record['tracker']=RecoveryTracker().data;record['tracker_hash']=canonical_hash(record['tracker']);record['requests']=0
  feedback=record['runtime']['feedback'];error=feedback.get('assessment_output_error')
  if error:
   responses=sorted((folder/'wire').glob('*_response.json'))
   if responses:
    body=read(responses[-1]).get('body',{});choices=body.get('choices',[])
    if choices:error['previous_output']=choices[0].get('message',{}).get('content','')
  record['runtime_hash']=canonical_hash(record['runtime'])
 p['task_state']=TaskState.from_dict(task).to_dict()
 p['pending_review']=deepcopy(original.get('pending_review'));p['pending_relation_work']=deepcopy(original.get('pending_relation_work'))
 p['open_tasks']=deepcopy(original['open_tasks'])
 engine=setup(destination,target,p,versions);proofs=deepcopy(p.get('signal_scope_migrations',[]));skipped=[]
 by_hash={r['record_hash']:r for r in p['signal_receipts']}
 for old_audit in audit['records']:
  r=by_hash[old_audit['receipt_record_hash']]
  try:proof=build_scope_migration(engine,r,old_audit,source_policy_hash=old_policy,reviewed_transition=transition)
  except ValueError as error:skipped.append({'signal_id':r['check'].get('id'),'reason':str(error)});continue
  if proof not in proofs:proofs.append(proof)
 p['signal_scope_migrations']=validate_scope_migrations(proofs,ledger=p['signal_receipts'])
 for field in ('current','history','lineage','signal_receipts','content_plan','identity','input_fingerprint','accepted_revision'):
  assert p[field]==original[field],field
 store=WorkspaceStore(target/'workspace.sqlite');store.save(ident,p,expected_revision=p['revision']);p=store.load(ident)
 assert p['task_state']['stage']=='assessing' and p['task_state']['draft']==old['draft']
 status=read(source/'status.json');write(destination/'status.json',{**status,'status':'prepared','checkpoint_migrated':True,'source_run':str(source),
  'pending_stage_preserved':'assessing','pending_writer_replayed':False,'errors':[]})
 write(destination/'review_assets.json',{**read(destination/'review_assets.json'),'本次后台续跑与checkpoint保全':'../'+report.name+'/index.html'})
 result={'database':plan['database'],'source':str(source),'destination':str(destination),'run_id':ident,'task_id':old['task_id'],
  'stage':'assessing','draft_unchanged':True,'candidate_hash_unchanged':p['task_state']['candidate_hash']==old['candidate_hash'],
  'accepted_tasks_preserved':len(original['history']),'accepted_receipts_preserved':len(original['signal_receipts']),
  'compatible_receipts':len(proofs),'skipped_receipts':skipped,'pending_writer_replayed':False,'blocked_attempts_archived':reset,
  'completed_batches_preserved':[k for k,v in migration['batches'].items() if v['status']=='completed_preserved']}
 write(destination/'assessment_resume_migration.json',result);write(destination/'receipt_policy_transition.json',transition)
 write(report/(plan['database']+'_migration.json'),result)
 print(json.dumps({k:v for k,v in result.items() if k!='blocked_attempts_archived'}),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['export','prepare']);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--audit',type=Path);parser.add_argument('--frozen-source',type=Path);parser.add_argument('--report',type=Path);a=parser.parse_args()
 if a.mode=='export':export(a.source.resolve(),a.output.resolve())
 else:prepare(a.source.resolve(),a.output.resolve(),a.audit.resolve(),a.frozen_source.resolve(),a.report.resolve())
