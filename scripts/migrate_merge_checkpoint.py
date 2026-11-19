"""Copy a stopped training merge into a new, explicitly migrated output.

No credentials or model calls are needed. Original inputs, accepted batches,
workspaces and audit logs remain untouched in the source directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import tempfile

from cortex.capabilities.graph_merge.integration.checkpoint_migration import migrate_checkpoint
from cortex.capabilities.graph_merge.integration.task_state import build_contract_versions, canonical_hash
from cortex.schema import load_root_schema


def _copy_training_output(source, destination, *, versions=None):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Migration requires a separate new output directory")
    if not (source / 'manifest.json').exists():
        raise ValueError("Source must be a prepared training output")
    versions = versions or build_contract_versions(load_root_schema())
    manifest = json.loads((source / 'manifest.json').read_text())
    destination.mkdir(parents=True)
    # Only graph/input artifacts are copied. No credential or worker config is
    # copied implicitly, and no symlink can make later writes reach the source.
    for name in ('manifest.json', 'merge_plan.json'):
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
    for key in ('merge_base_state', 'extra_evidence_path'):
        if manifest.get(key):
            relative = Path(manifest[key])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError(f"Unsafe manifest artifact path: {key}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / relative, target)
    for case in (source / 'cases').iterdir():
        if not case.is_dir():
            continue
        target = destination / 'cases' / case.name
        target.mkdir(parents=True)
        for name in ('input.json', 'state.json', 'status.json'):
            if (case / name).is_file():
                shutil.copy2(case / name, target / name)
    checkpoints = {}
    for batch in sorted((source / 'merge').glob('batch-*')):
        if not batch.is_dir():
            continue
        target = destination / 'merge' / batch.name
        target.mkdir(parents=True)
        for name in ('state.json', 'audit.json', 'result.json', 'input_fingerprint.json',
                     'case_input_mappings.json', 'incoming_state.json'):
            if (batch / name).is_file():
                shutil.copy2(batch / name, target / name)
        if (batch / 'state.json').exists():
            if not (batch / 'audit.json').is_file() or json.loads((batch / 'audit.json').read_text()).get('status') != 'passed':
                raise ValueError(f"Published batch {batch.name} lacks a passed audit")
            continue
        if (batch / 'workspace.sqlite').is_file():
            snapshot = migrate_checkpoint(batch / 'workspace.sqlite', target / 'workspace.sqlite', versions=versions)
            checkpoints[batch.name] = {'workspace': str(target / 'workspace.sqlite'),
                'run_id': snapshot['migration']['source_run_id'],
                'accepted_graph_hash': canonical_hash(snapshot['current']),
                'source_input_fingerprint': snapshot.get('input_fingerprint'),
                'source_sha256': snapshot['migration']['source_sha256']}
    plan = {'version': 1, 'source_output': str(source), 'destination_output': str(destination),
            'contract_versions': versions.to_dict(), 'checkpoints': checkpoints,
            'mode': 'merge_only', 'old_reviews_reused': False}
    (destination / 'merge_checkpoint_migration.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    return plan


def migrate_training_output(source, destination, *, versions=None):
    """Publish the new prepared directory only after every copy validates."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Migration requires a separate new output directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-migration-", dir=destination.parent))
    staged = temporary / "output"
    try:
        plan = _copy_training_output(source, staged, versions=versions)
        plan['destination_output'] = str(destination)
        for entry in plan['checkpoints'].values():
            entry['workspace'] = str(destination / Path(entry['workspace']).relative_to(staged))
        (staged / 'merge_checkpoint_migration.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
        staged.rename(destination)
        return plan
    finally:
        shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-output', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = migrate_training_output(args.source_output, args.output)
    print(json.dumps({'output': plan['destination_output'], 'migrated_batches': list(plan['checkpoints']),
                      'runner_argument': ['--mode', 'merge', '--merge-checkpoint-migration',
                                          str(args.output.resolve() / 'merge_checkpoint_migration.json')]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
