"""Reopen one stopped Writer schema-repair window without applying its output."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import argparse
import json
import os
import sqlite3

from cortex.capabilities.graph_merge.reconciliation.contract import EditDraft
from cortex.capabilities.graph_merge.runtime.state import TaskState, canonical_hash
from cortex.capabilities.graph_merge.runtime.contracts import build_contract_versions
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.recovery import RecoveryTracker
from cortex.schema import load_root_schema


def recover(root):
    status = json.loads((root/'status.json').read_text())
    assert status['status'] == 'needs_attention'
    try:
        os.kill(status['pid'], 0)
    except ProcessLookupError:
        pass
    else:
        raise ValueError('Worker is still alive')
    plan = json.loads((root/'plan.json').read_text())
    assert plan['contracts'] == build_contract_versions(load_root_schema()).to_dict()
    folders = [p for p in (root/'merge').glob('batch-*') if (p/'workspace.sqlite').exists() and not (p/'completed.json').exists()]
    assert len(folders) == 1
    folder = folders[0]
    run_id = f'continue-{plan["database"]}-{folder.name}'
    store = WorkspaceStore(folder/'workspace.sqlite')
    p = store.load(run_id); old = deepcopy(p); task = p['task_state']
    assert task['phase'] == 'reconcile' and task['stage'] == 'compiled_unsubmitted'
    assert not task['draft'].get('changes')
    response = sorted((folder/'wire').glob('*_response.json'))[-1]
    message = json.loads(response.read_text())['body']['choices'][0]['message']
    assert len(message['tool_calls']) == 1
    call = message['tool_calls'][0]['function']
    assert call['name'] == 'edit_draft' and json.loads(call['arguments']) == {'params': {}}
    EditDraft.model_validate({})
    archive = root/'recoveries'/('empty_edit_format_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    archive.mkdir(parents=True)
    with sqlite3.connect(folder/'workspace.sqlite') as source, sqlite3.connect(archive/'workspace.original.sqlite') as target:
        source.backup(target)
    (archive/'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2))
    updated = 0
    for record in task['agent_recovery'].values():
        if not isinstance(record, dict) or not record.get('tracker', {}).get('blocked'):
            continue
        blocked = record['tracker']['blocked']
        assert blocked['type'] == 'TaskNoProgress' and blocked['stage'] == 'output_schema'
        assert 'params' in str(record['tracker']['last_failure'])
        record['runtime']['feedback']['protocol_error'] = {
            'status': 'rejected_unapplied',
            'error': 'Unexpected top-level field params. The function arguments are the EditDraft object itself, not a wrapper.',
            'required_call': {'name': 'edit_draft', 'arguments': {'put_changes': [], 'remove_changes': []}},
            'explanation': 'For no change use those explicit empty arrays. Do not send params, patch, arguments, or empty fields. The rejected response was not applied; an independent Assessment is still required.'}
        record['runtime_hash'] = canonical_hash(record['runtime'])
        record['tracker'] = RecoveryTracker().data
        record['tracker_hash'] = canonical_hash(record['tracker'])
        assert record['requests'] < 12
        updated += 1
    assert updated == 1
    p['task_state'] = TaskState.from_dict(task).to_dict()
    for key in p:
        if key != 'task_state':
            assert p[key] == old[key], key
    for key in task:
        if key != 'agent_recovery':
            assert task[key] == old['task_state'][key], key
    store.save(run_id, p, expected_revision=p['revision'])
    audit = {'at': datetime.now().astimezone().isoformat(), 'archive': str(archive),
        'task_id': task['task_id'], 'accepted_tasks_preserved': len(p['history']),
        'accepted_signal_receipts_preserved': len(p['signal_receipts']),
        'accepted_graph_unchanged': True, 'draft_unchanged': True,
        'rejected_submission_applied': False, 'contracts_unchanged': True,
        'action': 'One formatting repair opportunity with explicit root-level empty arrays; no auto acceptance.'}
    (archive/'recovery.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    (root/'status.json').write_text(json.dumps({**status, 'status': 'prepared', 'errors': [],
        'format_recovery': str(archive)}, ensure_ascii=False, indent=2))
    print(json.dumps(audit, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--root', type=Path, required=True)
    recover(parser.parse_args().root.resolve())
