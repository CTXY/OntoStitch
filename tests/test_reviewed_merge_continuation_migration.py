"""A permission change reopens one unapplied task without replaying its prefix."""
import importlib.util
from pathlib import Path

import pytest

from cortex.capabilities.graph_merge.integration.task_state import ContractVersions, TaskState
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.schema import GraphNode, GraphSpace, GraphState


spec = importlib.util.spec_from_file_location('reviewed_continuation_migration',
    Path(__file__).parents[1] / 'scripts/migrate_reviewed_merge_continuation.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def fixture(source, status='needs_attention'):
    source.mkdir()
    old = ContractVersions(schema='schema', tools='old-tools', prompts='old-prompts', review_policy='policy')
    graph = GraphState(graph=GraphSpace(nodes=(GraphNode(id='N', node_type='concept', name='Accepted'),)))
    plan = {'database': 'toxicology', 'model': 'deepseek-v4-pro', 'base_case_ids': ['197'],
            'batches': [['217'], ['234']], 'contracts': old.to_dict()}
    runner.write(source / 'plan.json', plan)
    runner.write(source / 'status.json', {'status': status, 'merged_case_ids': ['197', '217'], 'pid': 111})
    runner.write(source / 'background_processes.json', {'worker': 111, 'monitor': 222})
    runner.write(source / 'approved_base.json', graph.model_dump(mode='json'))
    runner.write(source / 'source_evidence.json', {'bird-toxicology-234': {'question': 'Original evidence'}})
    complete = source / 'merge/batch-0001'
    complete.mkdir(parents=True)
    runner.write(complete / 'state.json', graph.model_dump(mode='json'))
    digest = runner.files_manifest(source)['merge/batch-0001/state.json']
    runner.write(complete / 'completed.json', {'case_ids': ['217'], 'state_sha256': digest})
    runner.write(complete / 'audit.json', {'status': 'passed'})
    pending = source / 'merge/batch-0002'
    pending.mkdir()
    task = TaskState.capture(graph, task_id='content-3', phase='consolidate', versions=old).with_draft(
        {'failure': 'Historical identity counterexample', 'explanation': 'Old contract cannot repair a self-edge'})
    payload = {'current': graph.model_dump(mode='json'), 'history': [
        {'task_id': f'content-{index}', 'phase': 'consolidate'} for index in range(3)],
        'lineage': {'version': 1, 'groups': []}, 'identity': {'groups': [], 'distinctions': []},
        'identity_read_ids': [], 'content_plan': [[{'members': ['N', 'other']}]],
        'reviewed_sources': {'N': []}, 'accepted_revision': 3, 'input_fingerprint': 'unchanged-input',
        'open_tasks': [{'id': 'content-3', 'phase': 'consolidate', 'targets': ['N'],
                        'groups': [{'members': ['N', 'other']}], 'checks': [], 'aliases': {'other': 'N'}}],
        'contract_versions': old.to_dict(), 'task_state': task.to_dict(), 'final': False}
    WorkspaceStore(pending / 'workspace.sqlite').save('continue-toxicology-batch-0002', payload)
    (pending / 'model_calls.jsonl').write_text('{"event_type":"model_end","historical":"exact source IO"}\n')
    return graph


def test_only_unfinished_task_reopened_and_all_historical_records_preserved(tmp_path):
    source, destination = tmp_path / 'original', tmp_path / 'migrated'
    fixture(source)
    before = runner.files_manifest(source)
    versions = ContractVersions(schema='schema', tools='new-tools', prompts='new-prompts', review_policy='policy')
    manifest = runner.migrate_reviewed_continuation(source, destination, versions=versions)
    assert runner.files_manifest(source) == before
    assert (destination / 'merge/batch-0001/state.json').read_bytes() == (source / 'merge/batch-0001/state.json').read_bytes()
    assert (destination / 'merge/batch-0002/model_calls.jsonl').read_bytes() == (source / 'merge/batch-0002/model_calls.jsonl').read_bytes()
    assert not (destination / 'background_processes.json').exists()
    assert runner.read(destination / 'migration_history/source_background_processes.json') == {'worker': 111, 'monitor': 222}
    assert runner.read(destination / 'plan.json')['contracts'] == versions.to_dict()
    assert manifest['batches']['batch-0001']['status'] == 'completed_preserved'
    assert manifest['batches']['batch-0002']['accepted_task_ids'] == ['content-0', 'content-1', 'content-2']
    saved = WorkspaceStore(destination / 'merge/batch-0002/workspace.sqlite').load('continue-toxicology-batch-0002')
    original = WorkspaceStore(source / 'merge/batch-0002/workspace.sqlite').load('continue-toxicology-batch-0002')
    for key in ('current', 'history', 'lineage', 'identity', 'content_plan', 'accepted_revision', 'input_fingerprint'):
        assert saved[key] == original[key]
    assert saved['task_state']['task_id'] == 'content-3'
    assert saved['task_state']['stage'] == 'writing'
    assert saved['task_state']['recovery'] == 'needs_rebuild'
    assert saved['task_state']['draft'] is None
    assert not saved['task_state']['agent_recovery']
    archive = runner.read(destination / 'merge/batch-0002/migration_source.json')
    assert archive['snapshot']['task_state']['draft']['failure'] == 'Historical identity counterexample'


def test_running_source_is_not_interrupted_or_migrated(tmp_path):
    source, destination = tmp_path / 'running', tmp_path / 'new'
    fixture(source, status='running')
    with pytest.raises(ValueError, match='stopped run'):
        runner.migrate_reviewed_continuation(source, destination)
    assert not destination.exists()


def test_failed_validation_leaves_no_published_destination(tmp_path):
    source, destination = tmp_path / 'original', tmp_path / 'new'
    fixture(source)
    runner.write(source / 'merge/batch-0001/audit.json', {'status': 'failed'})
    with pytest.raises(ValueError, match='matching state and passed audit'):
        runner.migrate_reviewed_continuation(source, destination)
    assert not destination.exists()


def test_completed_checkpoint_history_is_also_preserved(tmp_path):
    source,destination=tmp_path/'original',tmp_path/'migrated'
    graph=fixture(source)
    completed={'current':graph.model_dump(mode='json'),'lineage':{},
        'history':[{'task_id':'accepted-complete-task','phase':'reconcile'}],
        'open_tasks':[],'final':True}
    path=source/'merge/batch-0001/workspace.sqlite'
    WorkspaceStore(path).save('continue-toxicology-batch-0001',completed)
    versions=ContractVersions(schema='schema',tools='new',prompts='new',review_policy='policy')
    runner.migrate_reviewed_continuation(source,destination,versions=versions)
    assert WorkspaceStore(destination/'merge/batch-0001/workspace.sqlite').load(
        'continue-toxicology-batch-0001')==WorkspaceStore(path).load('continue-toxicology-batch-0001')


def test_historical_copy_has_independent_inode_and_identical_contents(tmp_path):
    original=tmp_path/'original.json';copied=tmp_path/'copied.json'
    original.write_text('historical record')
    runner.copy_artifact(original,copied)
    assert copied.read_bytes()==original.read_bytes()
    assert copied.stat().st_ino!=original.stat().st_ino
    copied.write_text('new receipt')
    assert original.read_text()=='historical record'


def test_repeated_migration_archives_prior_source_snapshot_without_overwrite(tmp_path):
    source, destination = tmp_path / 'original', tmp_path / 'migrated'
    fixture(source)
    old = source / 'merge/batch-0002/migration_source.json'
    old.write_text('{"original_prior_migration":"preserved exactly"}\n')
    manifest = runner.files_manifest(source)
    versions = ContractVersions(schema='schema', tools='new-tools', prompts='new-prompts', review_policy='policy')
    runner.migrate_reviewed_continuation(source, destination, versions=versions)
    assert runner.files_manifest(source) == manifest
    archived = list((destination / 'migration_history').glob('batch-0002_migration_source_*.json'))
    assert len(archived) == 1 and archived[0].read_bytes() == old.read_bytes()
    new = runner.read(destination / 'merge/batch-0002/migration_source.json')
    assert new['snapshot']['task_state']['task_id'] == 'content-3'
