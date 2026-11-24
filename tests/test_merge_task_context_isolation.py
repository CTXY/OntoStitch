"""Task frames isolate mutable execution state while sharing fixed input indexes."""
from copy import deepcopy

import pytest

from graph_merge_fixtures import catalog, concept, edge, state
from graph_merge_runtime_fixtures import prepare
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.runtime.context import fork_task_context
from cortex.capabilities.graph_merge.reconciliation.permissions import ReconciliationRuntime


def setup_frames():
    base = state([*catalog(), concept('A', 'a'), concept('B', 'b'), concept('C', 'c')],
                 [edge('e-A', 'A', 'a'), edge('e-B', 'B', 'b')])
    inputs, evidence_ids = prepare(base)
    def task(key):
        return LocalTask('task-' + key, 'reconcile', {key}, checks=[{
            'id': 'obligation-' + key, 'kind': 'node_relation_followup', 'mode': 'required',
            'node_ids': [key], 'edge_ids': ['e-' + key],
            'required_outcome': 'Preserve this explicitly nominated representation.',
        }])
    first_task, second_task = task('A'), task('B')
    first = fork_task_context(inputs, before=base, task=first_task)
    second = fork_task_context(inputs, before=base, task=second_task)
    return inputs, base, evidence_ids, first, second


def source_snapshot(inputs):
    return deepcopy({
        'nodes': {key: node.model_dump(mode='json') for key, node in inputs.nodes.items()},
        'raw_nodes': {key: node.model_dump(mode='json') for key, node in inputs.raw_nodes.items()},
        'edges': {key: edge.model_dump(mode='json') for key, edge in inputs.edges.items()},
        'raw_edges': {key: edge.model_dump(mode='json') for key, edge in inputs.raw_edges.items()},
        'adjacency': {key: [edge.id for edge in edges] for key, edges in inputs.adjacency.items()},
        'consumers': {key: sorted(ids) for key, ids in inputs.consumers.items()},
        'read_ids': inputs.read_ids, 'tool_log': inputs.tool_log, 'candidate': inputs.candidate,
    })


def test_task_candidates_callbacks_feedback_and_tool_reads_are_not_shared():
    inputs, base, _, first, second = setup_frames()
    original = source_snapshot(inputs)
    assert first.nodes is inputs.nodes and second.raw_nodes is inputs.raw_nodes
    assert first.read_ids is not second.read_ids and first.tool_log is not second.tool_log
    first.task_visible_ids.add('A')
    first.task_semantic_issues = ({'problem': 'Only first task'},)
    seen = []
    first.compile_task_patch = lambda value: seen.append(value)
    first.compile_task_patch('first draft')
    first.candidate = base.model_copy(update={'graph': base.graph.model_copy(update={
        'nodes': tuple(node.model_copy(update={'description': 'Unaccepted local candidate'})
                       if node.id == 'A' else node for node in base.graph.nodes),
    })})
    first.read_ids.add('A')
    first.log('read_nodes', {'ids': ['A']}, {'items': []})
    first.candidates('A')
    first.payload('identity')
    assert seen == ['first draft']
    assert 'A' not in second.task_visible_ids
    assert not second.task_semantic_issues
    assert getattr(second, 'compile_task_patch', None) is None
    assert second.candidate != first.candidate
    assert not second.tool_log
    assert source_snapshot(inputs) == original


def test_real_read_journals_evidence_and_write_permissions_are_task_local():
    inputs, base, evidence_ids, first, second = setup_frames()
    original = source_snapshot(inputs)
    first_rt = ReconciliationRuntime(first, {'task_id': first.active_task.id}, 'reconcile', system='Fixed role')
    second_rt = ReconciliationRuntime(second, {'task_id': second.active_task.id}, 'reconcile', system='Fixed role')
    first_id = next(ident for source, ident in evidence_ids.items() if source.source_id == 'A')
    second_id = next(ident for source, ident in evidence_ids.items() if source.source_id == 'B')
    assert first_rt.write_node_ids == {'A'} and second_rt.write_node_ids == {'B'}
    assert first_rt.write_edge_ids == {'e-A'} and second_rt.write_edge_ids == {'e-B'}
    second_before = second_rt.snapshot()
    first_rt.read_evidence(ids=[first_id])
    assert first_rt._journal and not second_rt._journal
    assert first_id in first_rt.evidence_view['id_to_body']
    assert first_id not in second_rt.evidence_view['id_to_body']
    first_before = first_rt.snapshot()
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        first_rt.read_evidence(ids=[first_id, second_id])
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        second_rt.read_evidence(ids=[first_id])
    assert first_rt.snapshot() == first_before and second_rt.snapshot() == second_before
    assert first.reconciliation_runtime is first_rt and second.reconciliation_runtime is second_rt
    assert source_snapshot(inputs) == original


def test_nested_task_frame_does_not_inherit_or_overwrite_parent_callbacks_and_grants():
    inputs, base, _, parent, _ = setup_frames()
    original = source_snapshot(inputs)
    calls = []
    parent.compile_task_patch = lambda value: calls.append(('parent', value))
    parent.task_visible_ids.add('A')
    parent.task_semantic_issues = ({'problem': 'Outer node finding'},)
    parent.active_draft = {'integrate_groups': [{'owner_id': 'A'}]}
    parent_runtime = object()
    parent.reconciliation_runtime = parent_runtime
    parent_reads = set(parent.read_ids)
    child = fork_task_context(inputs, before=base, task=LocalTask('nested', 'reconcile', {'B'}),
                              read_ids=parent.read_ids)
    assert getattr(child, 'compile_task_patch', None) is None
    assert getattr(child, 'reconciliation_runtime', None) is None
    assert not child.task_visible_ids and not child.task_semantic_issues
    child.compile_task_patch = lambda value: calls.append(('child', value))
    child.compile_task_patch('local graph draft')
    child.task_visible_ids.add('B')
    child.read_ids.add('C')
    child.task_semantic_issues = ({'problem': 'Inner relation finding'},)
    child.active_draft = {'changes': []}
    child.log('read_nodes', {'ids': ['B']}, {'items': []})
    parent.compile_task_patch('node draft')
    assert calls == [('child', 'local graph draft'), ('parent', 'node draft')]
    assert parent.task_visible_ids == {'A'} and parent.read_ids == parent_reads
    assert parent.task_semantic_issues == ({'problem': 'Outer node finding'},)
    assert parent.active_draft == {'integrate_groups': [{'owner_id': 'A'}]}
    assert parent.reconciliation_runtime is parent_runtime
    assert not parent.tool_log
    assert source_snapshot(inputs) == original


def test_same_graph_new_run_replaces_session_and_resets_progress_and_shared_budget():
    from graph_merge_fixtures import CurrentNoopAgent
    from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
    from cortex.schema import load_root_schema

    class Inspect(CurrentNoopAgent):
        max_calls = 8
        def __init__(self):
            self.sessions = []
            self.budgets = []
        def invoke(self, phase, context, payload):
            if phase == 'identity':
                session = self.engine.session
                assert not session.history and not session.signal_receipts and not session.relation_followups
                assert session.task_state is None and not session.open_tasks
                assert session.revision == 0 and session.storage_revision is None
                assert self.call_budget == {'used': 0, 'limit': 5}
                self.sessions.append(session)
                self.budgets.append(self.call_budget)
                self.call_budget['used'] += 1
            return super().invoke(phase, context, payload)
    agent = Inspect()
    engine = IncrementalGraphIntegrationEngine(agent, model_call_budget=5)
    agent.engine = engine
    graph = state([*catalog(), concept('A', 'a')], [edge('e-A', 'A', 'a')])
    first = engine.merge(state(), graph, load_root_schema(), run_id='first-run')
    previous = engine.session
    previous.history.append({'task_id': 'stale-progress'})
    previous.signal_receipts.append({'stale': True})
    previous.relation_followups.append({'stale': True})
    previous.task_state = object()
    agent.call_budget['used'] = 5
    second = engine.merge(state(), graph, load_root_schema(), run_id='second-run')
    assert first.state == second.state
    assert len(agent.sessions) == 2 and agent.sessions[0] is not agent.sessions[1]
    assert engine.session is not previous
    assert agent.budgets[0] is not agent.budgets[1]
    assert agent.call_budget['used'] == 1


def test_nested_relation_candidate_is_not_published_before_outer_node_acceptance(tmp_path, monkeypatch):
    from test_node_local_runtime import NodeChat, fixture, force_relation_handoff
    force_relation_handoff(monkeypatch)

    class Inspect(NodeChat):
        def __init__(self):
            super().__init__()
            self.inner_requests = 0
        def invoke(self, messages, config=None):
            assert self.engine.session.current == self.before
            assert not self.engine.session.history
            assert self.engine.execution.task is self.outer_task
            assert self.engine.execution.context.task_before == self.before
            assert self.engine.execution.context.active_task is self.outer_task
            local = ('node integration Writer' in messages[0].content
                     or 'independent node Assessment' in messages[0].content)
            if not local:
                self.inner_requests += 1
                assert 'integrate_groups' in self.engine.execution.context.active_draft
            return super().invoke(messages, config=config)
    chat = Inspect()
    engine, task = fixture(tmp_path, chat)
    before = chat.before = engine.session.current
    chat.outer_task = task
    source = source_snapshot(engine.session.inputs)
    engine._run_task(task, 'nested-isolation', aliases={'A': 'A', 'B': 'A'})
    assert chat.inner_requests == 2
    assert len(engine.session.history) == 1
    assert engine.session.current != before
    assert engine.execution.context.task_before == before
    assert engine.execution.context.active_task is task
    assert source_snapshot(engine.session.inputs) == source
