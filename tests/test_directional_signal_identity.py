"""Directed patterns must not collapse distinct signals to one public ID."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from graph_merge_fixtures import catalog, concept, edge, state
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.reconciliation.receipts import make_receipt, validate_receipt_ledger
from cortex.capabilities.graph_merge.reconciliation.scope import normalize_signal, signal_premise
from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.reconciliation.permissions import ReconciliationRuntime
from cortex.schema import load_root_schema


def fixture(kind):
    specs = [('AB','A','B'),('BA','B','A'),('BC','B','C'),('AC','A','C')]
    if kind == 'parallel_assertions':
        specs = [('AB','A','B'),('AB2','A','B'),('BA','B','A'),('BA2','B','A')]
    original=state([*catalog(),*(concept(k,'a') for k in ('A','B','C'))],
        [edge(i,a,b,'has_feature').model_copy(update={'properties':{
            'description':'Distinct retained assertion '+i}}) for i,a,b in specs])
    ctx=IntegrationContext(state(),original,load_root_schema())
    current=ctx.union_state()
    ctx.task_before=ctx.candidate=current
    checks=[row for row in inspection_signals(ctx,SimpleNamespace(state=current)) if row['kind']==kind]
    return ctx,current,checks


@pytest.mark.parametrize('kind',['triangle','parallel_assertions'])
def test_directed_detector_runtime_and_same_commit_ledger_keep_both_patterns(kind):
    ctx,current,checks=fixture(kind)
    assert len(checks)==2
    assert len({c['id'] for c in checks})==2
    assert len({tuple(c['edge_ids']) for c in checks})==2
    ctx.active_task=SimpleNamespace(id='directed-patterns',targets={n.id for n in current.graph.nodes
        if n.node_type=='concept'},checks=checks,feedback=[])
    runtime=ReconciliationRuntime(ctx,{'task_id':'directed-patterns'},'reconcile',system='fixed test policy')
    assert len(runtime.packet['signals'])==len(runtime.signal_ids)==2
    policy=canonical_hash('directional-receipt-policy')
    history={'task_id':'directed-patterns','phase':'reconcile','revision':1,'assessment':'graph_assess',
        'candidate_hash':canonical_hash(current),'reconciliation_receipt_policy_hash':policy,
        'reconciliation_check_hashes':[canonical_hash(normalize_signal(c)) for c in checks]}
    records=[make_receipt(c,premise_hash=canonical_hash(signal_premise(ctx,current,c)),
        policy_hash=policy,history_entry=history,candidate_hash=canonical_hash(current)) for c in checks]
    assert len(validate_receipt_ledger(records,history=[history],current=current))==2
    # Graph serialization order cannot rename either directed pattern.
    reordered=current.model_copy(update={'graph':current.graph.model_copy(update={
        'nodes':tuple(reversed(current.graph.nodes)),'edges':tuple(reversed(current.graph.edges))})})
    again=[row for row in inspection_signals(ctx,SimpleNamespace(state=reordered)) if row['kind']==kind]
    assert {c['id'] for c in again}=={c['id'] for c in checks}


def test_runtime_rejects_conflicting_duplicate_signal_ids_instead_of_silent_overwrite():
    ctx,current,checks=fixture('triangle')
    forged=deepcopy(checks)
    forged[1]['id']=forged[0]['id']
    ctx.active_task=SimpleNamespace(id='duplicate-signal',targets={n.id for n in current.graph.nodes
        if n.node_type=='concept'},checks=forged,feedback=[])
    with pytest.raises(ValueError,match='[Dd]uplicate|[Cc]onflicting|[Aa]mbiguous'):
        ReconciliationRuntime(ctx,{'task_id':'duplicate-signal'},'reconcile',system='fixed test policy')
