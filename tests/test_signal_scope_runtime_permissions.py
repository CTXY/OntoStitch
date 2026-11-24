"""Visible context never silently becomes a write or adjacency grant."""
from copy import deepcopy

import pytest
from test_reconciliation_context import node_context, runtime

from cortex.capabilities.graph_merge.reconciliation.contract import (
    DraftDocument,
)
from cortex.schema import GraphEdge, GraphNode


def with_boundary():
    ctx = node_context()
    graph = ctx.task_before.graph
    neighbor = GraphNode(id='boundary', node_type='concept', name='Independent boundary', properties={'definition': 'Independent meaning.'})
    edge = GraphEdge(id='outside-role', edge_type='is_a', from_node_id='N1', to_node_id=neighbor.id,
                     properties={'description': 'An existing business role.'})
    ctx.task_before = ctx.task_before.model_copy(update={'graph': graph.model_copy(update={
        'nodes': (*graph.nodes, neighbor), 'edges': (*graph.edges, edge)})})
    ctx.candidate = ctx.task_before
    return ctx


def test_actual_node_meaning_change_adds_affected_relations_to_assessment_only():
    ctx = with_boundary()
    writer = runtime(ctx=ctx)
    assert 'outside-role' not in writer.visible_edge_ids['before']
    assert 'boundary' not in writer.visible_node_ids['before']
    changed = tuple(n.model_copy(update={'properties': {**n.properties, 'definition': 'Changed business role.'}})
                    if n.id == 'N1' else n for n in ctx.task_before.graph.nodes)
    ctx.candidate = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={'nodes': changed})})
    assessment = runtime(ctx=ctx, phase='graph_assess')
    assert 'outside-role' in assessment.visible_edge_ids['candidate']
    assert 'boundary' in assessment.visible_node_ids['candidate']
    assert 'outside-role' not in assessment.write_edge_ids


def test_edge_description_update_does_not_expand_endpoints_into_hubs():
    ctx = with_boundary()
    ctx.candidate = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={
        'edges': tuple(e.model_copy(update={'properties': {'description': 'More precise relationship.'}})
                       if e.id == 'e1' else e for e in ctx.task_before.graph.edges)})})
    assessment = runtime(ctx=ctx, phase='graph_assess')
    assert 'outside-role' not in assessment.visible_edge_ids['candidate']
    assert 'boundary' not in assessment.visible_node_ids['candidate']


def test_metadata_only_owner_cannot_rewrite_unrelated_groups_or_meaning():
    ctx = with_boundary()
    groups = [{'edges': ['e1', 'outside-role'], 'description': 'Coupled knowledge.'},
              {'edges': ['unrelated-a', 'unrelated-b'], 'description': 'Unrelated group knowledge.'}]
    owner = GraphNode(id='metadata-owner', node_type='concept', name='Metadata owner',
                      properties={'definition': 'Unchanged meaning.', 'relation_metadata': groups})
    ctx.task_before = ctx.task_before.model_copy(update={'graph': ctx.task_before.graph.model_copy(update={
        'nodes': (*ctx.task_before.graph.nodes, owner)})})
    ctx.candidate = ctx.task_before
    rt = runtime(ctx=ctx)
    assert owner.id in rt._signal_scope.metadata_only_node_ids
    def validate(properties):
        raw = rt.draft.model_dump(mode='json')
        raw.update(version=rt.draft.version + 1, changes=[{'change_id': 'metadata', 'kind': 'node_update',
            'node_id': owner.id, 'set': {'properties': properties}, 'reason': 'Repair coupled metadata.'}])
        rt.set_draft(DraftDocument.model_validate(raw))
        return rt.validate_submission(rt.draft)
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        validate({'definition': 'Altered unrelated meaning.'})
    with pytest.raises(ValueError, match='OUT_OF_SCOPE'):
        validate({'relation_metadata': groups[:1]})
    revised = deepcopy(groups)
    revised[0]['description'] = 'Clarified coupled knowledge.'
    assert validate({'relation_metadata': revised})


def test_scoped_relation_read_recovery_cannot_restore_generic_adjacency():
    rt = runtime(ctx=with_boundary())
    rt.read_relations(node_ids=['N1'])
    assert 'outside-role' not in rt.visible_edge_ids['before']
    restored = runtime(ctx=with_boundary())
    restored.restore(rt.snapshot())
    assert restored.snapshot() == rt.snapshot()
    assert restored.write_edge_ids == rt.write_edge_ids


def test_reverted_draft_drops_effect_only_before_facts_without_accumulation():
    ctx = with_boundary()
    rt = runtime(ctx=ctx)
    prefix = rt.messages()[1].content
    graph = ctx.task_before.graph
    altered = ctx.task_before.model_copy(update={'graph': graph.model_copy(update={'nodes': tuple(
        n.model_copy(update={'properties': {**n.properties, 'definition': 'Temporarily changed meaning.'}})
        if n.id == 'N1' else n for n in graph.nodes)})})
    rt.set_compiled_candidate(altered, rt.draft.version)
    assert 'outside-role' in rt.visible_edge_ids['before']
    # A superseding complete draft no longer changes that definition.
    raw = rt.draft.model_dump(mode='json')
    raw.update(version=rt.draft.version + 1, changes=[])
    rt.set_draft(DraftDocument.model_validate(raw))
    rt.set_compiled_candidate(ctx.task_before, rt.draft.version)
    assert 'outside-role' not in rt.visible_edge_ids['before']
    assert 'boundary' not in rt.visible_node_ids['before']
    assert rt.messages()[1].content == prefix
    restored = runtime(ctx=with_boundary())
    restored.restore(rt.snapshot())
    assert restored.messages() == rt.messages()
