"""Unaccepted relation repairs survive restart, and receive a full review."""
import copy

import pytest
from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.graph.batch import BatchResult
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import TaskState, canonical_hash
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.consolidation.review import Review
from cortex.schema import load_root_schema


def setup_engine(agent, path, saved=None):
    incoming = state([*catalog(), concept("A", "a"), concept("B", "b")],
                     [edge("keep", "A", "B", "has_feature")])
    ctx = IntegrationContext(state(), incoming, load_root_schema())
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=0)
    engine.ctx, engine.current, engine.lineage = ctx, ctx.union_state(), LineageIndex(ctx)
    engine._revision, engine.reviewed_sources, engine.history = 0, {}, []
    engine.workspace_path = path
    engine._storage_revision = None
    engine.identity_decision = {}
    engine.pending_relation_work = None
    if saved:
        engine.task_state = TaskState.from_dict(saved['task_state'])
        engine.contract_versions = engine.task_state.versions
        engine.open_tasks = saved['open_tasks']
        engine._storage_revision = saved['revision']
    return engine, LocalTask('repair', 'reconcile', {ctx.input_node_map['A'], ctx.input_node_map['B']})


def test_unaccepted_draft_restores_and_review_covers_whole_transaction(tmp_path):
    class Agent:
        def __init__(self, resumed=False):
            self.resumed = resumed

        def invoke(self, phase, ctx, payload):
            keep, temp = ctx.input_edge_map['keep'], 'generated::temporary'
            if phase == 'reconcile':
                if self.resumed:
                    assert next(e for e in ctx.candidate.graph.edges if e.id == keep).properties['description'] == 'Accepted only after independent review'
                    raw = next(e for e in ctx.candidate.graph.edges if e.id == keep).model_dump(mode='json')
                    # The complete replacement draft keeps the valid change
                    # and omits the unaccepted temporary creation.
                    result = BatchResult.model_validate({'edits': [{'operation': 'update_edge', 'edge': raw}]})
                else:
                    raw = next(e for e in ctx.candidate.graph.edges if e.id == keep).model_dump(mode='json')
                    raw['properties']['description'] = 'Accepted only after independent review'
                    temporary = edge(temp, ctx.input_node_map['B'], ctx.input_node_map['A'], 'has_feature').model_dump(mode='json')
                    temporary['sources'] = next(n for n in ctx.candidate.graph.nodes if n.id == ctx.input_node_map['A']).model_dump(mode='json')['sources']
                    result = BatchResult.model_validate({'edits': [{'operation': 'update_edge', 'edge': raw}, {'operation': 'create_edge', 'edge': temporary}]})
                ctx.output_validator(result)
                return result
            assert phase == 'graph_assess'
            if self.resumed:
                assert keep in payload['actual_changes']['edges']['updated']
                assert temp not in payload['actual_changes']['edges']['created']
                assert temp not in payload['actual_changes']['edges']['deleted']
                return Review(verdict='accept')
            return Review.model_validate({'verdict': 'revise', 'issues': [{
                'refs': [{'kind': 'edge', 'id': temp}],
                'problem': 'The temporary assertion is unsupported.',
                'basis': [{'kind': 'edge', 'id': temp}],
                'required_outcome': 'Omit the unsupported creation from the replacement draft.'}]})

    path = tmp_path / 'workspace.sqlite'
    first, task = setup_engine(Agent(), path)
    original = first.current
    with pytest.raises(IntegrationIncomplete):
        first._run_task(task, 'draft')
    saved = WorkspaceStore(path).load('draft')
    assert first.current == original
    assert saved['current'] == original.model_dump(mode='json')
    resumed, task = setup_engine(Agent(True), path, saved)
    resumed._run_task(task, 'draft')
    assert resumed.pending_relation_work is None
    assert len(resumed.history) == 1
    assert not any(e.id == 'generated::temporary' for e in resumed.current.graph.edges)

    corrupt = copy.deepcopy(saved)
    physical = next(n for n in corrupt['task_state']['last_valid_candidate']['graph']['nodes'] if n['node_type'] == 'column')
    physical['name'] = 'tampered'
    with pytest.raises(ValueError, match='candidate hash'):
        setup_engine(Agent(True), tmp_path / 'broken.sqlite', corrupt)
    corrupt = copy.deepcopy(saved)
    semantic = next(n for n in corrupt['task_state']['base']['graph']['nodes'] if n['node_type'] == 'concept')
    semantic['description'] = 'A fabricated different review baseline'
    with pytest.raises(ValueError, match='base hash'):
        setup_engine(Agent(True), tmp_path / 'wrong-baseline.sqlite', corrupt)


def test_interrupted_relation_review_reuses_candidate_without_writer(tmp_path):
    class Interrupted:
        def invoke(self, phase, ctx, payload):
            if phase == 'graph_assess':
                raise RuntimeError('lost review connection')
            raw = next(e for e in ctx.candidate.graph.edges if e.id == ctx.input_edge_map['keep']).model_dump(mode='json')
            raw['properties']['description'] = 'Candidate awaiting independent review'
            result = BatchResult.model_validate({'edits': [{'operation': 'update_edge', 'edge': raw}]})
            ctx.output_validator(result)
            return result

    class ReviewOnly:
        def invoke(self, phase, ctx, payload):
            assert phase == 'graph_assess'
            assert ctx.input_edge_map['keep'] in payload['actual_changes']['edges']['updated']
            return Review(verdict='accept')

    path = tmp_path / 'workspace.sqlite'
    first, task = setup_engine(Interrupted(), path)
    original = first.current
    with pytest.raises(RuntimeError, match='lost review'):
        first._run_task(task, 'draft')
    saved = WorkspaceStore(path).load('draft')
    assert saved['current'] == original.model_dump(mode='json')
    assert saved['task_state']['stage'] == 'assessing'
    resumed, task = setup_engine(ReviewOnly(), path, saved)
    resumed._run_task(task, 'draft')
    assert resumed.history[0]['assessment'] == 'graph_assess'
    assert WorkspaceStore(path).load('draft')['pending_relation_work'] is None


def test_crash_after_accepted_review_reuses_review_without_model_calls(tmp_path):
    class Agent:
        def invoke(self, phase, ctx, payload):
            if phase == 'graph_assess':
                return Review(verdict='accept')
            raw = next(e for e in ctx.candidate.graph.edges if e.id == ctx.input_edge_map['keep']).model_dump(mode='json')
            raw['properties']['description'] = 'Reviewed, not yet committed'
            result = BatchResult.model_validate({'edits': [{'operation': 'update_edge', 'edge': raw}]})
            ctx.output_validator(result)
            return result

    class NoModel:
        def invoke(self, *args, **kwargs):
            raise AssertionError('A compiled and accepted current review must not rerun the model')

    path = tmp_path / 'workspace.sqlite'
    first, task = setup_engine(Agent(), path)
    original = first.current
    persist = first._persist

    def crash_after_review(run_id, final=False):
        persist(run_id, final=final)
        if first.task_state.stage == 'ready_to_commit':
            raise RuntimeError('crash before commit')

    first._persist = crash_after_review
    with pytest.raises(RuntimeError, match='crash before commit'):
        first._run_task(task, 'draft')
    saved = WorkspaceStore(path).load('draft')
    assert saved['task_state']['stage'] == 'ready_to_commit'
    assert saved['current'] == original.model_dump(mode='json') and not saved['history']
    resumed, task = setup_engine(NoModel(), path, saved)
    resumed._run_task(task, 'draft')
    assert len(resumed.history) == 1
    assert next(e for e in resumed.current.graph.edges if e.id == resumed.ctx.input_edge_map['keep']).properties['description'] == 'Reviewed, not yet committed'


def test_failed_resume_compilation_preserves_saved_candidate_and_draft(tmp_path):
    class Interrupted:
        def invoke(self, phase, ctx, payload):
            if phase == 'graph_assess':
                raise RuntimeError('interrupted review')
            raw = next(e for e in ctx.candidate.graph.edges if e.id == ctx.input_edge_map['keep']).model_dump(mode='json')
            raw['properties']['description'] = 'The actual proposed change'
            return BatchResult.model_validate({'edits': [{'operation': 'update_edge', 'edge': raw}]})

    class NoModel:
        def invoke(self, *args, **kwargs):
            raise AssertionError('Resume must validate the saved draft first')

    path = tmp_path / 'workspace.sqlite'
    first, task = setup_engine(Interrupted(), path)
    with pytest.raises(RuntimeError, match='interrupted review'):
        first._run_task(task, 'draft')
    store = WorkspaceStore(path)
    inconsistent = store.load('draft')
    candidate = inconsistent['task_state']['last_valid_candidate']
    next(n for n in candidate['graph']['nodes'] if n['node_type'] == 'concept')['name'] = 'Not produced by this draft'
    inconsistent['task_state']['candidate_hash'] = canonical_hash(candidate)
    store.save('draft', inconsistent, expected_revision=inconsistent['revision'])
    saved = store.load('draft')
    resumed, task = setup_engine(NoModel(), path, saved)
    with pytest.raises(ValueError, match='no longer compiles'):
        resumed._run_task(task, 'draft')
    assert store.load('draft')['task_state'] == saved['task_state']
    assert store.load('draft')['current'] == saved['current']
