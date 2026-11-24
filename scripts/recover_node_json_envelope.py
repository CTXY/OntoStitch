"""Restart a stopped node serialization recovery after the exact-fence parser fix.

Archives the entire checkpoint and retry state; never applies the rejected
submission or changes accepted graph/history. Only the failed retry window is
reopened, retaining its original local dialogue and evidence.
"""
import argparse
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json
import os
import sqlite3

from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.runtime.state import TaskState,build_contract_versions,canonical_hash
from cortex.capabilities.graph_merge.llm.recovery import RecoveryTracker
from cortex.capabilities.graph_merge.consolidation.contract import NodeWriterResult
from cortex.capabilities.graph_merge.reconciliation.agent import _json_object
from cortex.schema import load_root_schema


def recover(root):
 status=json.loads((root/'status.json').read_text());plan=json.loads((root/'plan.json').read_text())
 if status['status']!='needs_attention':raise ValueError('Recover a stopped worker only')
 try:os.kill(status['pid'],0)
 except ProcessLookupError:pass
 else:raise ValueError('Worker is still alive')
 if plan['contracts']!=build_contract_versions(load_root_schema()).to_dict():raise ValueError('Contract migration required')
 folders=sorted(p for p in (root/'merge').glob('batch-*') if (p/'workspace.sqlite').exists() and not (p/'completed.json').exists())
 if len(folders)!=1:raise ValueError('Require one incomplete batch')
 folder=folders[0];run_id=f'continue-{plan["database"]}-{folder.name}';store=WorkspaceStore(folder/'workspace.sqlite');p=store.load(run_id);old=deepcopy(p)
 task=p['task_state']
 if task['phase']!='consolidate' or task['stage']!='writing' or task['draft'] is not None:raise ValueError('Require a node result rejected before any draft was saved')
 response=sorted((folder/'wire').glob('*_response.json'))[-1]
 text=json.loads(response.read_text())['body']['choices'][0]['message']['content']
 if not text.strip().startswith('```'):raise ValueError('Last output was not an exact JSON envelope')
 NodeWriterResult.model_validate(_json_object(text))
 archive=root/'recoveries'/('exact_json_envelope_'+datetime.now().strftime('%Y%m%d_%H%M%S'));archive.mkdir(parents=True)
 with sqlite3.connect(folder/'workspace.sqlite') as source,sqlite3.connect(archive/'workspace.original.sqlite') as target:source.backup(target)
 (archive/'status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
 reset=[]
 for key,record in task['agent_recovery'].items():
  if record.get('scope',{}).get('phase')!='consolidate':continue
  blocked=record['tracker'].get('blocked')
  if not blocked or blocked['type']!='OutputProtocolRecoveryExhausted':raise ValueError('Only exhausted JSON protocol recovery can be reopened')
  reset.append({'key':key,'previous_tracker':deepcopy(record['tracker'])})
  record['tracker']=RecoveryTracker().data;record['tracker']['fallback']=True
  record['tracker_hash']=canonical_hash(record['tracker'])
 if not reset:raise ValueError('No eligible failed Writer')
 p['task_state']=TaskState.from_dict(task).to_dict()
 for field in ('current','history','lineage','signal_receipts','content_plan','identity','input_fingerprint','accepted_revision'):
  assert p[field]==old[field],field
 store.save(run_id,p,expected_revision=p['revision'])
 audit={'task_id':task['task_id'],'batch':folder.name,'archive':str(archive),'accepted_tasks_preserved':len(p['history']),
        'accepted_graph_hash':canonical_hash(p['current']),'rejected_submission_applied':False,'retry_reset':reset,
        'exact_response_schema_valid':True,'response':str(response),'contracts_unchanged':True}
 (archive/'recovery.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
 (root/'status.json').write_text(json.dumps({**status,'status':'prepared','errors':[],'serialization_recovery':str(archive)},ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({k:v for k,v in audit.items() if k!='retry_reset'},ensure_ascii=False),flush=True)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args();recover(args.root.resolve())
