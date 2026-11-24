"""A diagnosed node self-edge is visible to Assessment, never silently removed."""
from copy import deepcopy

import pytest

from graph_merge_fixtures import catalog, concept, edge, state
from cortex.capabilities.graph_merge.graph.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.schema import GraphState, GraphSpace, load_root_schema


def preview_batch():
    before = state([*catalog(), concept('A', 'a'), concept('B', 'a')],
                   [edge('E', 'A', 'B', 'is_a')])
    context = IntegrationContext(before, GraphState(graph=GraphSpace()), load_root_schema())
    context.task_before = before
    context.node_local_compile = True
    batch = ActiveBatch(context, before, targets={'A'}, aliases={'A': 'A', 'B': 'A'})
    diagnostic = {'code': 'alias_created_self_relation', 'edge_id': 'E', 'owner_id': 'A',
                  'original_from_node_id': 'A', 'original_to_node_id': 'B',
                  'from_node_id': 'A', 'to_node_id': 'A', 'edge_type': 'is_a',
                  'required_outcome': 'Remove the erroneous relation or retain B separately.'}
    batch.node_structural_diagnostics = [diagnostic]
    return before, context, batch


def test_diagnosed_self_edge_remains_in_pending_candidate():
    before, context, batch = preview_batch()
    batch.submit_atomic(BatchResult())
    pending = next(edge for edge in batch.preview.graph.edges if edge.id == 'E')
    assert pending.id == 'E' and pending.from_node_id == pending.to_node_id == 'A'
    assert len(batch.node_structural_diagnostics) == 1
    assert context.existing == before
    assert next(edge for edge in before.graph.edges if edge.id == 'E').to_node_id == 'B'


def test_unflagged_self_edge_still_fails_strict_schema_validation():
    _, _, batch = preview_batch()
    batch.node_structural_diagnostics = []
    with pytest.raises(ValueError, match='requires distinct endpoints'):
        batch.submit_atomic(BatchResult())
    assert batch.preview is None


def test_diagnostic_cannot_conceal_an_unrelated_unknown_endpoint():
    _, _, batch = preview_batch()
    bad = edge('OTHER', 'A', 'missing', 'is_a')
    batch.seed = batch.seed.model_copy(update={'graph': batch.seed.graph.model_copy(update={
        'edges': (*batch.seed.graph.edges, bad)})})
    with pytest.raises(ValueError, match='unknown endpoint'):
        batch.submit_atomic(BatchResult())
    assert batch.preview is None


def test_forged_original_endpoint_diagnostic_is_rejected():
    _, _, batch = preview_batch()
    batch.node_structural_diagnostics[0]['original_to_node_id'] = 'A'
    with pytest.raises(ValueError, match='Invalid controller diagnostic'):
        batch.submit_atomic(BatchResult())


def test_self_edge_diagnostic_cannot_escape_its_fixed_group():
    _, _, batch = preview_batch()
    batch.node_original_group = {'owner_id': 'A', 'member_ids': ['A', 'outside']}
    with pytest.raises(ValueError, match='Invalid controller diagnostic'):
        batch.submit_atomic(BatchResult())


def test_explicit_relation_deletion_clears_pending_diagnostic():
    _, _, batch = preview_batch()
    declared = deepcopy(batch.node_structural_diagnostics)
    batch.submit_atomic(BatchResult.model_validate({'edits': [
        {'operation': 'delete_edge', 'edge_id': 'E'}], 'changes': [
        {'before': [{'kind': 'edge', 'id': 'E'}], 'after': [{'kind': 'node', 'id': 'A'}],
         'explanation': 'The original classification is redundant after valid identity integration.'}]}))
    assert not any(edge.id == 'E' for edge in batch.preview.graph.edges)
    assert not batch.node_structural_diagnostics
    assert declared[0]['edge_id'] == 'E'


@pytest.mark.parametrize('repair', ['remove_relation', 'keep_member_separate'])
@pytest.mark.parametrize('restore_ready_accept', [False, True])
def test_assessment_sees_self_edge_and_cannot_accept_it_before_writer_repairs(tmp_path, repair, restore_ready_accept):
    from graph_merge_runtime_fixtures import engine_for
    from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch
    from cortex.capabilities.graph_merge.consolidation.review import Review
    from cortex.capabilities.graph_merge.models import LocalTask
    from cortex.capabilities.graph_merge.runtime.state import TaskState
    from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
    from cortex.capabilities.graph_merge.consolidation.presentation import build_node_packet

    base, _, _ = preview_batch()

    class Agent:
        node_local_contract = True
        writes = 0
        phases = []
        interrupt = restore_ready_accept

        def invoke(self, phase, context, payload):
            self.phases.append(phase)
            if phase == 'node_assess':
                if self.writes == 1:
                    assert context.node_structural_diagnostics[0]['edge_id'] == 'E'
                    assert any(e.id == 'E' and e.from_node_id == e.to_node_id
                               for e in context.candidate.graph.edges)
                    if self.interrupt:
                        self.interrupt = False
                        raise RuntimeError('Pause before Assessment result')
                else:
                    assert not context.node_structural_diagnostics
                # Deliberately wrong for the initial candidate: controller
                # must return structural feedback rather than commit it.
                return Review(verdict='accept')
            self.writes += 1
            fixed = build_node_packet(context, payload, before=context.task_before)
            if self.writes > 1:
                assert 'self-relation' in str(payload['issues'])
                if repair == 'keep_member_separate':
                    raw = {'keep_separate_ids': ['A', 'B'],
                        'explanation': 'Retain the distinct original nodes and their relation.'}
                    context.node_local_raw = deepcopy(raw)
                    return MergePatch.model_validate(raw)
            member = fixed['group']['members'][0]
            raw = {'integrate_groups': [{'owner_id': 'A', 'member_ids': ['A', 'B'],
                'content': {key: deepcopy(member[key]) for key in
                            ('name', 'description', 'aliases', 'tags', 'properties')},
                'source_routes': [{'source_unit_id': unit['unit_id'],
                                  'destination': {'kind': 'result'}, 'explanation': 'Retain knowledge.'}
                                 for unit in fixed['source_unit_manifest'][0]['source_units']],
                'explanation': 'Integrate compatible definitions.'}], 'explanation': 'Local candidate.'}
            if self.writes > 1:
                raw['relation_removals'] = [{'edge_id': 'E', 'reason': 'Redundant internal classification; preserved in the merged identity.'}]
            context.node_local_raw = deepcopy(raw)
            return MergePatch.model_validate(raw)

    agent = Agent()
    engine, _ = engine_for(base, agent, tmp_path / 'preview.sqlite')
    task = LocalTask('node-preview', 'consolidate', {'A', 'B'},
                     groups=[{'members': ['A', 'B'], 'reason': 'Tentative same identity.'}])
    if restore_ready_accept:
        with pytest.raises(RuntimeError, match='Pause before Assessment'):
            engine._run_task(task, 'preview', aliases={'A': 'A', 'B': 'A'})
        # A malformed/stale saved acceptance must not skip the real schema
        # publication gate when it is restored without another model review.
        engine.session.task_state = engine.session.task_state.with_review(Review(verdict='accept'),
            candidate_hash=engine.session.task_state.candidate_hash)
        engine._persist('preview')
        saved = WorkspaceStore(engine.workspace_path).load('preview')
        engine.session.task_state = TaskState.from_dict(saved['task_state'])
    engine._run_task(task, 'preview', aliases={'A': 'A', 'B': 'A'})
    assert agent.phases == ['consolidate', 'node_assess', 'consolidate', 'node_assess']
    assert len(engine.session.history) == 1
    if repair == 'keep_member_separate':
        assert engine.session.current == base
    else:
        assert not any(edge.id == 'E' for edge in engine.session.current.graph.edges)
        assert 'B' not in {node.id for node in engine.session.current.graph.nodes}
