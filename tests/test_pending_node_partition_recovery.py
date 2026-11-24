from copy import deepcopy

import pytest
from recover_pending_node_partition import partition_plan


def fixture():
    def part(members):
        return {'owner_id': members[0], 'member_ids': members,
            'content': {'name': members[0], 'description': 'A proposed identity.', 'aliases': [], 'tags': [], 'properties': {'definition': 'A proposed identity.'}},
            'source_routes': [], 'explanation': 'Model proposed a distinct identity.'}
    return {'identity': {'groups': [{'members': ['A', 'B', 'C', 'D'], 'meaning': 'Original', 'reason': 'Prior grouping'}]},
        'history': [{'task_id': 'content-0'}], 'current': {'retained': True},
        'content_plan': [[{'members': ['X', 'Y']}], [{'members': ['A', 'B', 'C', 'D']}], [{'members': ['E', 'F']}]],
        'task_state': {'phase': 'consolidate', 'task_id': 'content-1', 'draft': {'integrate_groups': [part(['A', 'B']), part(['C', 'D'])]}}}


def test_exact_partition_creates_single_group_tasks_without_applying_draft():
    source = fixture()
    snapshot = deepcopy(source)
    result, audit = partition_plan(source)
    assert source == snapshot
    assert result['history'] == source['history'] and result['current'] == source['current']
    assert result['content_plan'][1][0]['members'] == ['A', 'B']
    assert result['content_plan'][2] == source['content_plan'][2]
    assert result['content_plan'][3][0]['members'] == ['C', 'D']
    assert audit['appended_task_ids'] == ['content-3'] and not audit['rejected_draft_applied']


@pytest.mark.parametrize('members', [['B', 'C'], ['C', 'outside'], ['C', 'C']])
def test_overlap_outside_or_missing_members_are_rejected(members):
    source = fixture()
    source['task_state']['draft']['integrate_groups'][1]['member_ids'] = members
    source['task_state']['draft']['integrate_groups'][1]['owner_id'] = members[0]
    with pytest.raises(ValueError):
        partition_plan(source)


def test_previously_accepted_task_cannot_be_reopened():
    source = fixture()
    source['history'].append({'task_id': 'content-1'})
    with pytest.raises(ValueError, match='accepted'):
        partition_plan(source)


def test_repaired_checkpoint_preserves_accepts_and_original_file(tmp_path):
    import hashlib

    from recover_pending_node_partition import repair
    from cortex.capabilities.graph_merge.inputs import IntegrationContext
    from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
    from cortex.capabilities.graph_merge.runtime.state import ContractVersions, TaskState
    from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
    from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema

    folder = tmp_path / 'batch'
    folder.mkdir()
    graph = GraphState(graph=GraphSpace(nodes=tuple(GraphNode(id=k, node_type='concept', name=k, properties={'targets': [], 'definition': 'A test concept.'}) for k in 'ABCD')))
    incoming = GraphState(graph=GraphSpace())
    for name, value in [('before.json', graph), ('incoming.json', incoming)]:
        (folder / name).write_text(value.model_dump_json())
    ctx = IntegrationContext(graph, incoming, load_root_schema())
    payload = fixture()
    versions = ContractVersions(schema='schema', tools='tools', prompts='prompts', review_policy='policy')
    task = TaskState.capture(graph, task_id='content-1', phase='consolidate', versions=versions).with_draft(
        payload['task_state']['draft'])
    payload.update(current=graph.model_dump(mode='json'), lineage=LineageIndex(ctx).snapshot(),
        task_state=task.to_dict(), contract_versions=versions.to_dict(), identity_read_ids=list('ABCD'),
        accepted_revision=1, input_fingerprint='unchanged', open_tasks=[], final=False)
    source = folder / 'workspace.sqlite'
    WorkspaceStore(source).save('run', payload)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / 'recovery/workspace.sqlite'
    audit = repair(source, destination, run_id='run')
    saved = WorkspaceStore(destination).load('run')
    original = WorkspaceStore(source).load('run')
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    for key in ('current', 'history', 'lineage', 'accepted_revision', 'input_fingerprint'):
        assert saved[key] == original[key]
    assert saved['task_state']['stage'] == 'writing' and not saved['task_state']['agent_recovery']
    assert saved['task_state']['draft'] is None
    assert audit['appended_task_ids'] == ['content-3']
