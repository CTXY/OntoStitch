"""Clone a stopped reviewed-merge run and reopen only its unfinished task.

Original checkpoints, accepted tasks and complete model I/O remain immutable.
The new run retains the same source inputs and run IDs; only active contracts
and the unapplied task are explicitly migrated.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from cortex.capabilities.graph_merge.integration.checkpoint_migration import migrate_checkpoint
from cortex.capabilities.graph_merge.runtime.state import build_contract_versions, canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.schema import GraphState, load_root_schema


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def copy_artifact(source, destination):
    """Independent historical records; use APFS clones to avoid duplicate disk use."""
    if sys.platform == 'darwin':
        result = subprocess.run(['/bin/cp', '-c', '-p', str(source), str(destination)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return str(destination)
    return shutil.copy2(source, destination)


def copy_checkpoint(source, destination):
    with sqlite3.connect(source) as origin, sqlite3.connect(destination) as target:
        origin.backup(target)


def files_manifest(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Migration does not follow symbolic links')
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def migrate_reviewed_continuation(source, destination, *, versions=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if (not source.is_dir() or destination.exists() or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise ValueError('Migration requires an existing source and a separate new destination')
    plan, status = read(source / 'plan.json'), read(source / 'status.json')
    if status['status'] not in {'needs_attention', 'stopped'}:
        raise ValueError('Only a stopped run may be migrated; no worker is interrupted automatically')
    if not plan.get('database') or not isinstance(plan.get('batches'), list):
        raise ValueError('Source is not a reviewed merge continuation')
    versions = versions or build_contract_versions(load_root_schema())
    source_files = files_manifest(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f'.{destination.name}-migration-', dir=destination.parent))
    staged = temporary / 'run'
    entries = {}
    try:
        # Full historical I/O is copied with independent inodes. SQLite is
        # rebuilt by the dedicated migration helper, never copied as a live DB.
        def ignored(_directory, names):
            return [name for name in names if name in
                    {'workspace.sqlite', 'workspace.sqlite-wal', 'workspace.sqlite-shm'}]
        shutil.copytree(source, staged, ignore=ignored, copy_function=copy_artifact)
        archive = staged / 'migration_history'
        archive.mkdir(exist_ok=True)
        for name in ('plan.json', 'status.json', 'background_processes.json', 'runtime.json'):
            if (staged / name).exists():
                shutil.copyfile(staged / name, archive / ('source_' + name))
        (staged / 'background_processes.json').unlink(missing_ok=True)
        (staged / 'runtime.json').unlink(missing_ok=True)
        for index, case_ids in enumerate(plan['batches'], 1):
            batch_name = f'batch-{index:04d}'
            batch, target = source / 'merge' / batch_name, staged / 'merge' / batch_name
            if not batch.exists():
                continue
            if (batch / 'completed.json').exists():
                complete = read(batch / 'completed.json')
                digest = hashlib.sha256((batch / 'state.json').read_bytes()).hexdigest()
                if complete['state_sha256'] != digest or read(batch / 'audit.json').get('status') != 'passed':
                    raise ValueError(f'Completed batch {batch_name} lacks matching state and passed audit')
                GraphState.model_validate(read(batch / 'state.json'))
                if (batch / 'workspace.sqlite').exists():
                    copy_checkpoint(batch / 'workspace.sqlite', target / 'workspace.sqlite')
                entries[batch_name] = {'status': 'completed_preserved', 'case_ids': case_ids,
                                       'state_sha256': digest}
                continue
            if not (batch / 'workspace.sqlite').exists():
                continue
            run_id = f'continue-{plan["database"]}-{batch_name}'
            before = WorkspaceStore(batch / 'workspace.sqlite').load(run_id)
            if before is None:
                raise ValueError(f'Missing exact saved run ID {run_id}')
            old_task = before.get('task_state')
            # Repeated migrations retain the earlier source snapshot under a
            # distinct archive name before creating this migration's snapshot.
            previous_artifact = target / 'migration_source.json'
            if previous_artifact.exists():
                digest = hashlib.sha256(previous_artifact.read_bytes()).hexdigest()
                archived_artifact = archive / f'{batch_name}_migration_source_{digest}.json'
                if archived_artifact.exists():
                    if archived_artifact.read_bytes() != previous_artifact.read_bytes():
                        raise ValueError('Earlier migration artifact archive differs')
                    previous_artifact.unlink()
                else:
                    previous_artifact.rename(archived_artifact)
            after = migrate_checkpoint(batch / 'workspace.sqlite', target / 'workspace.sqlite',
                                       versions=versions, run_id=run_id)
            for field in ('current', 'history', 'lineage', 'identity', 'content_plan',
                          'reviewed_sources', 'accepted_revision', 'input_fingerprint'):
                if before.get(field) != after.get(field):
                    raise ValueError(f'Migration changed accepted progress: {field}')
            if old_task and after['task_state']['task_id'] != old_task['task_id']:
                raise ValueError('Migration changed the pending task identity')
            entries[batch_name] = {'status': 'unfinished_task_reopened', 'case_ids': case_ids,
                'run_id': run_id, 'task_id': (old_task or {}).get('task_id'),
                'source_storage_revision': before['revision'],
                'accepted_revision': before['accepted_revision'],
                'accepted_task_ids': [item['task_id'] for item in before['history']],
                'accepted_graph_hash': canonical_hash(before['current']),
                'source_input_fingerprint': before['input_fingerprint']}
        if not any(row['status'] == 'unfinished_task_reopened' for row in entries.values()):
            raise ValueError('No unfinished checkpoint was found to migrate')
        updated_plan = deepcopy(plan)
        updated_plan['contracts'] = versions.to_dict()
        updated_plan['checkpoint_migration'] = 'checkpoint_migration.json'
        write(staged / 'plan.json', updated_plan)
        write(staged / 'status.json', {'status': 'prepared', 'model': plan['model'],
            'merged_case_ids': status.get('merged_case_ids', plan['base_case_ids']),
            'source_run': str(source), 'checkpoint_migrated': True,
            'completed_tasks_retained': sum(len(e.get('accepted_task_ids', [])) for e in entries.values())})
        migration = {'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
            'source_output': str(source), 'destination_output': str(destination),
            'source_files': source_files, 'contract_versions': versions.to_dict(),
            'batches': entries, 'source_records_preserved': True,
            'historical_model_io_copied': True, 'old_unapplied_candidate_accepted': False}
        write(staged / 'checkpoint_migration.json', migration)
        if files_manifest(source) != source_files:
            raise RuntimeError('Source changed during migration; no new run was published')
        staged.rename(destination)
        return migration
    finally:
        shutil.rmtree(temporary)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-output', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    result = migrate_reviewed_continuation(arguments.source_output, arguments.output)
    print(json.dumps({'output': result['destination_output'], 'batches': result['batches']}, ensure_ascii=False))
