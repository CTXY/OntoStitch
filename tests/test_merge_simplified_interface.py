"""Controlled contract tests; no model/network or case-specific semantic rules."""
from types import SimpleNamespace

import pytest
from graph_merge_fixtures import catalog, concept, edge, state
from graph_merge_chat import Chat

from cortex.capabilities.graph_merge.graph.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.evidence import (
    EvidencePresentation,
)
from cortex.capabilities.graph_merge.graph.scope import (
    ProposalScope,
    ScopeContextRequired,
)
from cortex.capabilities.graph_merge.reconciliation.context import (
    relation_context,
)
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.consolidation.review import Review as NodeReview
from cortex.capabilities.graph_merge.evidence import EvidenceItem
from cortex.schema import SourceReference, load_root_schema


def region(visible=True):
    graph = state([*catalog(), *(concept(k, 'a') for k in ('A', 'B', 'X', 'Y'))],
                  [edge('ab', 'A', 'B', 'has_feature'), edge('xy', 'X', 'Y', 'has_feature')])
    ctx = IntegrationContext(state(), graph, load_root_schema())
    ctx.candidate = ctx.union_state()
    ids = ctx.input_node_map
    batch = ActiveBatch(ctx, ctx.candidate, targets={ids['A']})
    ctx.active_batch = batch
    payload = relation_context(ctx, ctx.candidate, LocalTask('local', 'reconcile', {ids['A']}))
    if not visible:
        payload['nodes'] = [r for r in payload['nodes'] if r['id'] != ids['B']]
    scope = ProposalScope(ctx, payload, EvidencePresentation(ctx, payload['evidence']))
    batch.prepare_submission = scope.prepare
    return ctx, ids, scope, payload


def rename(key):
    return BatchResult(edits=[{'operation': 'update_node', 'node': {'id': key, 'name': 'Representative referent'}}])


def test_missing_coupled_context_returned_before_any_mutation():
    ctx, ids, _, _ = region(visible=False)
    with pytest.raises(ScopeContextRequired) as error:
        ctx.active_batch.submit_atomic(rename(ids['B']))
    assert error.value.context['nodes'][0]['id'] == ids['B']
    assert not ctx.active_batch.pending and ctx.active_batch.preview is None
    assert ids['B'] not in ctx.active_batch.allowed
    ctx.active_batch.submit_atomic(rename(ids['B']))
    assert ids['B'] in ctx.active_batch.allowed


def test_reading_unrelated_object_never_grants_permission():
    ctx, ids, scope, _ = region()
    scope.observe([{'id': ids['X']}])
    with pytest.raises(ValueError, match='Outside this local task'):
        ctx.active_batch.submit_atomic(rename(ids['X']))
    assert not ctx.active_batch.pending and ids['X'] not in ctx.active_batch.allowed


def test_explicit_local_replacement_can_include_an_isolated_successor():
    ctx, ids, _, _ = region()
    successor = next(n for n in ctx.candidate.graph.nodes if n.id == ids['X'])
    result = BatchResult.model_validate({
        'edits': [{'operation': 'update_node', 'node': successor.model_dump(mode='json')}],
        'changes': [{'before': [{'kind': 'node', 'id': ids['A']}],
                     'after': [{'kind': 'node', 'id': ids['X']}],
                     'explanation': 'Explicitly identify the retained representation of the local assertion.'}],
    })
    with pytest.raises(ScopeContextRequired) as error:
        ctx.active_batch.submit_atomic(result)
    assert ids['X'] in {row['id'] for row in error.value.context['nodes']}
    assert ids['X'] not in ctx.active_batch.allowed
    admitted = ctx.active_batch.prepare_submission(result)
    assert ids['X'] in admitted
    assert ids['Y'] not in admitted
    assert not ctx.active_batch.pending  # Scope approval does not execute the replacement.


def test_invalid_patch_rolls_back_temporary_scope_and_edits():
    ctx, ids, _, _ = region()
    before = set(ctx.active_batch.allowed)
    result = rename(ids['B']).model_copy(update={'edits': (*rename(ids['B']).edits,
        BatchResult(edits=[{'operation': 'update_edge', 'edge': {'id': ctx.input_edge_map['ab'], 'to_node_id': 'missing'}}]).edits[0])})
    with pytest.raises(ValueError):
        ctx.active_batch.submit_atomic(result)
    assert ctx.active_batch.allowed == before
    assert not ctx.active_batch.pending and not ctx.active_batch.scope_events


def evidence_context():
    first, second = SourceReference(source_id='A'), SourceReference(source_id='B')
    body = {'question': 'Shared question', 'sql': 'SELECT id FROM objects'}
    return SimpleNamespace(evidence=[EvidenceItem(id='Q1', source=first, case_id=None, content=body),
                                    EvidenceItem(id='Q2', source=second, case_id=None, content=body)]), first, second


def test_evidence_bodies_deduplicate_without_merging_attribution():
    ctx, first, second = evidence_context()
    reader = EvidencePresentation(ctx)
    rows = reader.read(['Q1', 'Q2'])['items']
    assert len(rows) == 1 and rows[0]['evidence_ids'] == ['Q1', 'Q2']
    assert reader.ids_for([first]) == ['Q1'] and reader.ids_for([second]) == ['Q2']
    assert not reader.read(['Q1', 'Q2'])['items']
    assert EvidencePresentation(ctx).read(['Q1'])['items'][0]['content']


def test_later_evidence_alias_reuses_body_without_losing_its_id():
    ctx, _, _ = evidence_context()
    reader = EvidencePresentation(ctx)
    reader.read(['Q1'])
    packet = reader.append({'items': [{'id': 'edge', 'evidence_ids': ['Q2']}]}, ['Q2'])
    assert packet['evidence'] == [{'id': 'Q2', 'same_content_as': 'Q1'}]
    assert list(packet)[-1] == 'evidence'


def test_same_evidence_id_with_different_content_fails_explicitly():
    ctx, _, _ = evidence_context()
    ctx.evidence[1] = ctx.evidence[1].model_copy(update={'id': 'Q1', 'content': {'sql': 'different'}})
    with pytest.raises(ValueError, match='Conflicting content'):
        EvidencePresentation(ctx)


def test_review_schema_contains_no_source_truth_checklist():
    assert set(NodeReview.model_json_schema()['properties']) == {'verdict', 'issues'}
    with pytest.raises(ValueError):
        NodeReview.model_validate({'verdict': 'accept', 'rationale_checks': [], 'decision_checks': []})


@pytest.mark.parametrize('phase', ['consolidate', 'node_assess'])
def test_node_roles_refuse_unplanned_singleton_without_widening_tools(phase):
    # The former broad node interface accepted arbitrary singleton/whole-graph
    # packets. The new two-role contract requires a complete identity group.
    ctx, _, _, _ = region()
    ctx.task_before = ctx.candidate
    ctx.task_identity_groups = []
    chat = Chat([])
    with pytest.raises(ValueError, match='exactly one complete identity group'):
        IncrementalIntegrationAgent(chat).invoke(phase, ctx, {'task_id': 'unplanned'})
    assert not chat.requests
