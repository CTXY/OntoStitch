"""Unaccepted relation repairs survive restart, and receive a full review."""
import copy

import pytest
from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.incremental import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.reconciliation import RelationReview
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
        engine.pending_relation_work = saved['pending_relation_work']
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
                    result = BatchResult.model_validate({'edits': [{'operation': 'delete_edge', 'edge_id': temp}],
                        'changes': [{'before': [{'kind': 'edge', 'id': temp}], 'after': [], 'explanation': 'Remove unsupported temporary assertion.'}]})
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
                return RelationReview(verdict='accept')
            return RelationReview.model_validate({'verdict': 'revise', 'issues': ['Remove temporary assertion.'], 'issue_refs': [{'kind': 'edge', 'id': temp}]})

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
    physical = next(n for n in corrupt['pending_relation_work']['state']['graph']['nodes'] if n['node_type'] == 'column')
    physical['name'] = 'tampered'
    broken, task = setup_engine(Agent(True), tmp_path / 'broken.sqlite', corrupt)
    with pytest.raises(ValueError, match='protected physical'):
        broken._run_task(task, 'draft')
    corrupt = copy.deepcopy(saved)
    semantic = next(n for n in corrupt['pending_relation_work']['review_baseline']['graph']['nodes'] if n['node_type'] == 'concept')
    semantic['description'] = 'A fabricated different review baseline'
    broken, task = setup_engine(Agent(True), tmp_path / 'wrong-baseline.sqlite', corrupt)
    with pytest.raises(ValueError, match='original accepted baseline'):
        broken._run_task(task, 'draft')


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
            return RelationReview(verdict='accept')

    path = tmp_path / 'workspace.sqlite'
    first, task = setup_engine(Interrupted(), path)
    original = first.current
    with pytest.raises(RuntimeError, match='lost review'):
        first._run_task(task, 'draft')
    saved = WorkspaceStore(path).load('draft')
    assert saved['current'] == original.model_dump(mode='json')
    assert saved['pending_relation_work']['status'] == 'unaccepted_pending_independent_review'
    resumed, task = setup_engine(ReviewOnly(), path, saved)
    resumed._run_task(task, 'draft')
    assert resumed.history[0]['assessment'] == 'graph_assess'
    assert WorkspaceStore(path).load('draft')['pending_relation_work'] is None
