"""Workflow success does not falsely certify a rejected deletion proposal."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path

import pytest
from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.runtime.state import ContractVersions, TaskState
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore


spec = importlib.util.spec_from_file_location('node_trajectory_review',
    Path(__file__).parents[1] / 'scripts/review_node_revision_trajectory.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def trajectory(tmp_path):
    owner, separate, relation = runner.OWNER, runner.SEPARATE, runner.EDGE
    pair = 'input::input::n198'
    before = state([*catalog(), concept(owner, 'a'), concept(pair, 'a'), concept(separate, 'a')],
                   [edge(relation, pair, separate, 'is_a')])
    after = before.model_copy(update={'graph': before.graph.model_copy(update={
        'nodes': tuple(n for n in before.graph.nodes if n.id != pair),
        'edges': tuple(e.model_copy(update={'from_node_id': owner}) if e.id == relation else e
                       for e in before.graph.edges)})})
    folder = tmp_path / 'remove_relation'
    folder.mkdir()
    runner.write(folder / 'before.json', before.model_dump(mode='json'))
    runner.write(folder / 'after.json', after.model_dump(mode='json'))
    content = {k: v for k, v in concept(owner, 'a').model_dump(mode='json').items()
               if k in {'name', 'description', 'aliases', 'tags', 'properties'}}
    draft = {'keep_separate_ids': [separate], 'integrate_groups': [{'owner_id': owner,
        'member_ids': [owner, pair], 'content': content}], 'explanation': 'Keep the atom involvement separate.'}
    versions = ContractVersions(schema='schema', tools='tools', prompts='prompts', review_policy='policy')
    task = TaskState.capture(before, task_id='content-3', phase='consolidate', versions=versions)
    task = task.with_draft(draft).with_candidate(after)
    task = replace(task.with_review({'verdict': 'accept', 'issues': []}, candidate_hash=task.candidate_hash), stage='committed')
    prefix = [f'content-{i}' for i in range(3)]
    WorkspaceStore(folder / 'workspace.sqlite').save('real-node-revision:remove_relation', {
        'current': after.model_dump(mode='json'), 'lineage': {}, 'open_tasks': [],
        'history': [{'task_id': k} for k in [*prefix, 'content-3']],
        'contract_versions': versions.to_dict(), 'task_state': task.to_dict()})
    runner.write(folder / 'source_scope.json', {'accepted_prefix_tasks': prefix})
    runner.write(folder / 'result.json', {'path': 'remove_relation', 'status': 'needs_attention',
                                         'errors': [{'type': 'AssertionError', 'message': ''}]})
    runner.write(tmp_path / 'results.json', [{'path': 'remove_relation', 'status': 'needs_attention'},
                                            {'path': 'keep_separate', 'status': 'passed'}])
    events = []

    def start(ident, phase, packet):
        events.append({'event_type': 'model_start', 'component': phase, 'call_id': ident,
                       'messages': [{'content': 'Current round:\n' + json.dumps(packet)}]})

    def end(ident, phase, name, output):
        events.append({'event_type': 'model_end', 'component': phase, 'call_id': ident,
                       'message': {'tool_calls': [{'name': name, 'args': output}]}})

    removals = [{'edge_id': relation, 'reason': 'Proposed redundant relation'}]
    start('w1', 'integration_consolidate', {})
    end('w1', 'integration_consolidate', 'submit_patch', {'relation_removals': removals})
    start('a1', 'integration_node_assess', {'relation_removals': removals,
        'candidate': {'nodes': [{'id': owner}], 'relations': []}})
    end('a1', 'integration_node_assess', 'Review', {'verdict': 'revise', 'issues': [{
        'problem': 'Deletion would hide the mistaken group.',
        'basis': [{'id': relation}, {'id': separate}], 'required_outcome': 'Keep the member separate.'}]})
    start('w2', 'integration_consolidate', {})
    end('w2', 'integration_consolidate', 'submit_patch', draft)
    start('a2', 'integration_node_assess', {'candidate': {'nodes': [{'id': owner}, {'id': separate}],
        'relations': [e.model_dump(mode='json') for e in after.graph.edges]}})
    end('a2', 'integration_node_assess', 'Review', {'verdict': 'accept', 'issues': []})
    (folder / 'model_calls.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
    return folder


def test_preserves_rejection_and_labels_accepted_grouping_correction_truthfully(tmp_path):
    folder = trajectory(tmp_path)
    checkpoint_hash = runner.digest(folder / 'workspace.sqlite')
    outcome = runner.review(folder)
    assert outcome['status'] == 'passed'
    assert outcome['tested_path'] == 'remove_relation'
    assert outcome['final_resolution'] == 'keep_separate'
    assert outcome['removal_proposal_accepted_by_assessment'] is False
    assert runner.read(folder / 'result.json')['final_review']['verdict'] == 'accept'
    assert runner.read(folder / 'trajectory_history/original_runner_result.json')['status'] == 'needs_attention'
    assert runner.digest(folder / 'workspace.sqlite') == checkpoint_hash
    assert runner.review(folder) == outcome


def test_requires_actual_final_model_acceptance_not_only_committed_snapshot(tmp_path):
    folder = trajectory(tmp_path)
    trace = folder / 'model_calls.jsonl'
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    events[-1]['message']['tool_calls'][0]['args']['verdict'] = 'revise'
    trace.write_text(''.join(json.dumps(e) + '\n' for e in events))
    with pytest.raises(ValueError, match='Missing real compiled deletion review'):
        runner.review(folder)
    assert runner.read(folder / 'result.json')['status'] == 'needs_attention'
    assert not (folder / 'trajectory_review.json').exists()


def test_does_not_touch_an_unfinished_real_test(tmp_path):
    folder = trajectory(tmp_path)
    (tmp_path / 'results.json').unlink()
    with pytest.raises(ValueError, match='runner to finish'):
        runner.review(folder)
    assert not (folder / 'trajectory_history').exists()
