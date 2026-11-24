"""Exercise both local corrections across real compile/review/checkpoint gates."""
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from test_graph_integration import catalog, concept, edge, state
from test_merge_task_review_boundary import engine_for

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import TaskState
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.schema import GraphState, SourceReference, load_root_schema


class LocalRevisionChat:
    def __init__(self, path, *, interrupt_assessment=False, accept_invalid=False):
        self.path = path
        self.interrupt_assessment = interrupt_assessment
        self.accept_invalid = accept_invalid
        self.writer_calls = 0
        self.assessment_calls = 0
        self.packets = []

    def bind(self, **_kwargs):
        return self

    def bind_tools(self, tools, **_kwargs):
        self.names = {tool.name for tool in tools}
        return self

    def invoke(self, messages, config=None):
        fixed = json.loads(messages[2].content.split('\n', 1)[1])
        current = json.loads(messages[-1].content.split('\n', 1)[1])
        packet = {**fixed, **current}
        self.packets.append(deepcopy(packet))
        if 'submit_patch' in self.names:
            self.writer_calls += 1
            group = packet['group']
            separate = ['C'] if self.writer_calls > 1 and self.path == 'separate' else []
            retained = [ident for ident in group['member_ids'] if ident not in separate]
            members = {member['id']: member for member in group['members']}
            content = {key: deepcopy(members['A'][key]) for key in
                       ('name', 'description', 'aliases', 'tags', 'properties')}
            content['description'] = 'The complete local pairing content and the recorded involvement meaning.'
            content['properties']['definition'] = content['description']
            manifest = packet['source_unit_manifest'][0]['source_units']
            units = [unit for unit in manifest if unit['node_id'] in retained]
            result = {'integrate_groups': [{'owner_id': 'A', 'member_ids': retained,
                'content': content, 'source_routes': [{'source_unit_id': unit['unit_id'],
                    'destination': {'kind': 'result'}, 'explanation': 'Preserve this complete input.'}
                    for unit in units], 'explanation': 'Integrate the retained local identity.'}],
                'explanation': 'Revise only this original group.'}
            if separate:
                result['keep_separate_ids'] = separate
            if self.writer_calls > 1 and self.path == 'remove':
                result['relation_removals'] = [{'edge_id': 'is-a',
                    'reason': 'The same-identity relation is redundant after this merge; its recorded involvement meaning is retained in the complete result.'}]
            if self.writer_calls > 1:
                assert 'is-a' in str(packet), 'Assessment diagnostic must survive the return to Writer'
            return AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': f'writer-{self.writer_calls}', 'args': result}])
        assert 'Review' in self.names
        self.assessment_calls += 1
        candidate = packet['candidate']
        relations = {row['id']: row for row in candidate['relations']}
        diagnostics = packet.get('structural_diagnostics', [])
        if diagnostics:
            assert relations['is-a']['from_node_id'] == relations['is-a']['to_node_id'] == 'A'
            assert any(row['id'] == 'is-a' for row in packet['before_relations'])
            if self.interrupt_assessment:
                self.interrupt_assessment = False
                raise RuntimeError('Controlled stop before Assessment result')
            review = {'verdict': 'accept', 'issues': []} if self.accept_invalid else {
                'verdict': 'revise', 'issues': [{'refs': [{'kind': 'edge', 'id': 'is-a', 'view': 'candidate'}],
                    'basis': [{'kind': 'edge', 'id': 'is-a', 'view': 'before'}],
                    'problem': 'The merged local relation is a forbidden self-edge.',
                    'required_outcome': ('Delete only the redundant is-a relation, preserving its supported meaning and source.'
                        if self.path == 'remove' else 'Keep C as an unchanged standalone node and retain the directed is-a relationship to it.')}]
            }
        else:
            if self.path == 'remove':
                assert 'is-a' not in relations
            else:
                assert relations['is-a']['from_node_id'] == 'A'
                assert relations['is-a']['to_node_id'] == 'C'
                assert {row['id'] for row in candidate['nodes']} == {'A', 'C'}
            review = {'verdict': 'accept', 'issues': []}
        return AIMessage(content='', tool_calls=[{'name': 'Review', 'id': f'review-{self.assessment_calls}', 'args': review}])


def local_engine(tmp_path, chat):
    relation = edge('is-a', 'B', 'C', 'is_a').model_copy(update={
        'sources': (SourceReference(source_id='relation-evidence'),)})
    base = state([*catalog(), concept('A', 'a'), concept('B', 'a'), concept('C', 'a')], [relation])
    engine, _ = engine_for(base, IncrementalIntegrationAgent(chat, max_calls=6), tmp_path / 'node.sqlite')
    engine.max_rounds = 3
    engine.history = [{'task_id': f'content-{index}', 'phase': 'consolidate', 'assessment': 'node_assess'}
                      for index in range(3)]
    engine._revision = 3
    task = LocalTask('content-3', 'consolidate', {'A'}, groups=[{
        'members': ['A', 'B', 'C'], 'meaning': 'A proposed local pairing identity',
        'reason': 'The three source descriptions were initially grouped together.'}])
    return engine, task, base


def restore_engine(tmp_path, chat, saved):
    engine, task, base = local_engine(tmp_path, chat)
    engine.current = GraphState.model_validate(saved['current'])
    engine.lineage = LineageIndex.restore(engine.ctx, saved['lineage'])
    engine.history = deepcopy(saved['history'])
    engine._revision = saved['accepted_revision']
    engine._storage_revision = saved['revision']
    engine.task_state = TaskState.from_dict(saved['task_state'])
    engine.contract_versions = engine.task_state.versions
    return engine, task, base


@pytest.mark.parametrize('path', ['remove', 'separate'])
def test_assessment_feedback_repairs_relation_or_preserves_excluded_node(tmp_path, path):
    chat = LocalRevisionChat(path)
    engine, task, base = local_engine(tmp_path, chat)
    prefix = deepcopy(engine.history)
    engine._run_task(task, 'node-revision', aliases={'A': 'A', 'B': 'A', 'C': 'A'})
    GraphManager(load_root_schema()).validate_state(engine.current)
    assert chat.writer_calls == 2 and chat.assessment_calls == 2
    assert engine.history[:3] == prefix and len(engine.history) == 4
    indexed = {node.id: node for node in engine.current.graph.nodes}
    if path == 'separate':
        assert indexed['C'] == next(node for node in base.graph.nodes if node.id == 'C')
        assert engine.lineage.node_map.get('C', 'C') == 'C'
    else:
        assert 'C' not in indexed
        assert SourceReference(source_id='relation-evidence') in indexed['A'].sources
    saved = WorkspaceStore(engine.workspace_path).load('node-revision')
    assert saved['task_state']['stage'] == 'committed'
    assert saved['accepted_revision'] == 4


def test_invalid_preview_checkpoint_resumes_assessment_without_replaying_writer(tmp_path):
    chat = LocalRevisionChat('remove', interrupt_assessment=True)
    engine, task, base = local_engine(tmp_path, chat)
    aliases = {'A': 'A', 'B': 'A', 'C': 'A'}
    with pytest.raises(RuntimeError, match='Controlled stop'):
        engine._run_task(task, 'node-revision', aliases=aliases)
    saved = WorkspaceStore(engine.workspace_path).load('node-revision')
    assert saved['task_state']['stage'] == 'assessing'
    assert saved['current'] == base.model_dump(mode='json')
    assert chat.writer_calls == 1
    restored, task, _ = restore_engine(tmp_path, chat, saved)
    restored._run_task(task, 'node-revision', aliases=aliases)
    assert chat.writer_calls == 2, 'Only the subsequent feedback-driven revision may invoke Writer again'
    assert len(restored.history) == 4
    assert restored.history[:3] == saved['history']
    GraphManager(load_root_schema()).validate_state(restored.current)


def test_assessment_accept_cannot_commit_a_known_invalid_self_edge(tmp_path):
    chat = LocalRevisionChat('remove', accept_invalid=True)
    engine, task, _ = local_engine(tmp_path, chat)
    engine._run_task(task, 'node-revision', aliases={'A': 'A', 'B': 'A', 'C': 'A'})
    assert chat.writer_calls == 2, 'Controller must return structural feedback despite the first accept'
    assert not any(e.id == 'is-a' for e in engine.current.graph.edges)
    GraphManager(load_root_schema()).validate_state(engine.current)


def test_restored_ready_acceptance_cannot_bypass_current_structural_guard(tmp_path):
    chat = LocalRevisionChat('remove', interrupt_assessment=True)
    engine, task, base = local_engine(tmp_path, chat)
    aliases = {'A': 'A', 'B': 'A', 'C': 'A'}
    with pytest.raises(RuntimeError, match='Controlled stop'):
        engine._run_task(task, 'node-revision', aliases=aliases)
    store = WorkspaceStore(engine.workspace_path)
    saved = store.load('node-revision')
    pending = TaskState.from_dict(saved['task_state'])
    accepted = pending.with_review({'verdict': 'accept', 'issues': []}, candidate_hash=pending.candidate_hash)
    saved['task_state'] = accepted.to_dict()
    store.save('node-revision', saved, expected_revision=saved['revision'])
    saved = store.load('node-revision')
    assert saved['task_state']['stage'] == 'ready_to_commit'
    assert saved['current'] == base.model_dump(mode='json')
    restored, task, _ = restore_engine(tmp_path, chat, saved)
    restored._run_task(task, 'node-revision', aliases=aliases)
    assert chat.writer_calls == 2, 'The saved accept must produce feedback, not an invalid commit'
    assert chat.assessment_calls == 2, 'Only the repaired result needs another Assessment'
    assert len(restored.history) == 4 and restored.history[:3] == saved['history']
    assert not any(e.id == 'is-a' for e in restored.current.graph.edges)
    GraphManager(load_root_schema()).validate_state(restored.current)
