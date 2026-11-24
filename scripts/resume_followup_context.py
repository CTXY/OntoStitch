"""Migrate a stopped stale-handoff Assessment to an isolated corrected runtime.

Keeps the same run/task IDs, draft, candidate, graph and accepted history.
Only the task's historical-to-current material and compatible runtime bindings
change; a fresh independent Assessment must still accept the actual candidate.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json,os,sqlite3

from resume_multi_result_merges import setup
from cortex.capabilities.graph_merge.integration.task_state import TaskState,build_contract_versions,canonical_hash
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.relation_followups import followup_task,validate_followup_queue
from cortex.capabilities.graph_merge.integration.signal_scope_migration import build_scope_migration,validate_scope_migrations
from cortex.capabilities.graph_merge.integration.signal_receipts import policy_hash
from cortex.schema import load_root_schema


def read(p):return json.loads(p.read_text())
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
def migrate(root,report):
 status=read(root/'status.json');plan=read(root/'plan.json')
 if status['status']!='needs_attention':raise ValueError('Only a stopped run can be migrated')
 try:os.kill(status['pid'],0)
 except ProcessLookupError:pass
 else:raise ValueError('Worker is alive')
 folders=sorted(f for f in (root/'merge').glob('batch-*') if (f/'workspace.sqlite').exists() and not (f/'completed.json').exists())
 if len(folders)!=1:raise ValueError('Require one pending batch')
 folder=folders[0];run_id=f'continue-{plan["database"]}-{folder.name}';store=WorkspaceStore(folder/'workspace.sqlite');p=store.load(run_id);old=deepcopy(p)
 task=p['task_state'];assert task['phase']=='reconcile' and task['stage']=='assessing'
 assert task['draft'] and task['candidate_draft_id']==task['draft_id']
 audit=read(report/'thrombosis_old_facts.json');assert audit['source_current_hash']==canonical_hash(p['current'])
 versions=build_contract_versions(load_root_schema())
 engine=setup(root,folder,p,versions);engine.lineage=LineageIndex.restore(engine.ctx,p['lineage'])
 queue=validate_followup_queue(p['relation_followups']);row=next(row for row in queue if 'node-followup-'+row['id'][:20]==task['task_id'])
 revised=followup_task(engine,row)
 definition={'id':revised.id,'phase':revised.phase,'targets':sorted(revised.targets),'groups':revised.groups,
             'checks':revised.checks,'feedback':revised.feedback,'aliases':{}}
 before_root=report/'runtime_before';after_root=report/'runtime_after';files={}
 for path in (after_root/'src/cortex/capabilities/graph_merge').rglob('*'):
  if not path.is_file() or path.suffix not in ('.py','.md','.json'):continue
  relative=path.relative_to(after_root);before=before_root/relative
  if before.exists() and before.read_bytes()!=path.read_bytes():files[str(relative)]={'before':sha256(before.read_bytes()).hexdigest(),'after':sha256(path.read_bytes()).hexdigest()}
 transition={'source_policy_hash':canonical_hash({'receipt_protocol':1,'contracts':audit['source_contract_versions']}),
             'target_policy_hash':policy_hash(engine),'files':files}
 proofs=deepcopy(p.get('signal_scope_migrations',[]));records={r['record_hash']:r for r in p['signal_receipts']}
 for record in audit['records']:
  proof=build_scope_migration(engine,records[record['receipt_record_hash']],record,
      source_policy_hash=transition['source_policy_hash'],reviewed_transition=transition)
  if proof not in proofs:proofs.append(proof)
 archive=root/'recoveries'/('followup_context_'+datetime.now().strftime('%Y%m%d_%H%M%S'));archive.mkdir(parents=True)
 with sqlite3.connect(folder/'workspace.sqlite') as source,sqlite3.connect(archive/'workspace.original.sqlite') as target:source.backup(target)
 for name in ('status.json','plan.json','background_processes.json'):
  if (root/name).exists():(archive/name).write_bytes((root/name).read_bytes())
 write(archive/'original_pending_task.json',{'task_state':old['task_state'],'open_tasks':old['open_tasks']})
 task['versions']=versions.to_dict();task['compilation_binding']=None
 # The old rejection was made without the verified retirement/provenance facts.
 # Archive it above; do not re-inject it as a current, already-proven defect.
 # A new independent review still has to validate the actual candidate.
 task['semantic_issues']=[];task['semantic_issue_basis']=[];task['issues']=[];task['issues_draft_id']=None;task['review']=None
 task['agent_recovery']={}
 definition['checks'][0]['check_question']='Does the current graph already satisfy the historical node-stage outcome, including the original evidence, or does it need a further local repair?'
 p['task_state']=TaskState.from_dict(task).to_dict();p['contract_versions']=versions.to_dict()
 p['open_tasks']=[definition if r['id']==revised.id else r for r in p['open_tasks']]
 p['signal_scope_migrations']=validate_scope_migrations(proofs,ledger=p['signal_receipts'])
 for field in ('current','history','lineage','identity','content_plan','signal_receipts','accepted_revision','input_fingerprint','relation_followups'):
  assert p[field]==old[field],field
 assert task['draft']==old['task_state']['draft'] and task['candidate_hash']==old['task_state']['candidate_hash']
 store.save(run_id,p,expected_revision=p['revision'])
 updated={**plan,'contracts':versions.to_dict()};temporary=root/'plan.migration.tmp';write(temporary,updated);temporary.replace(root/'plan.json')
 write(root/'runtime_source.json',{'src':str(after_root/'src'),'source_snapshot':str(after_root),'reason':'Historical node followups must resolve current relation-merger aliases; do not restart the healthy toxicology worker.'})
 write(root/'status.json',{**status,'status':'prepared','errors':[],'pending_stage_preserved':'assessing','runtime_source':str(after_root/'src')})
 result={'run_id':run_id,'task_id':task['task_id'],'archive':str(archive),'accepted_tasks_preserved':len(p['history']),
         'accepted_receipts_preserved':len(p['signal_receipts']),'compatible_receipts':len(proofs),
         'draft_unchanged':True,'candidate_unchanged':True,'stage':'assessing','writer_replayed':False,
         'old_task_definition':next(r for r in old['open_tasks'] if r['id']==revised.id),'new_task_definition':definition,
         'runtime_source':str(after_root/'src'),'transition':transition}
 write(report/'thrombosis_recovery.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ('old_task_definition','new_task_definition','transition')},ensure_ascii=False),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);parser.add_argument('--report',type=Path,required=True);a=parser.parse_args();migrate(a.root.resolve(),a.report.resolve())
