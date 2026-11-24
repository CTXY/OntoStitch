"""Read-only replay of accepted live receipts; never creates a model client.

The scheduler replay contains only already accepted checks, with alternate
grouping. Related/unrelated controls operate on detached in-memory graphs.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.inputs import input_fingerprint
from cortex.capabilities.graph_merge.planning import _signal_targets
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.reconciliation.receipts import is_accepted
from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals
from cortex.capabilities.graph_merge.reconciliation.contract import SIGNALS
from cortex.capabilities.graph_merge.graph.publication import Preview
from cortex.capabilities.graph_merge.runtime.state import ContractVersions
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.sql_training.graph_merge_evidence import MappingEvidenceRepository, SqlTrainingGraphMergeEvidenceResolver
from cortex.schema import GraphNode, GraphState, load_root_schema


def audit(root, *, batch=None):
    root=Path(root).resolve()
    plan=json.loads((root/'plan.json').read_text())
    status=json.loads((root/'status.json').read_text())
    index=batch if batch is not None else status.get('batch')
    if index is None:
        raise ValueError('No active batch is recorded')
    folder=root/'merge'/f'batch-{index:04d}'
    cp=WorkspaceStore(folder/'workspace.sqlite').load(f'continue-{plan["database"]}-batch-{index:04d}')
    before=GraphState.model_validate(json.loads((folder/'before.json').read_text()))
    incoming=GraphState.model_validate(json.loads((folder/'incoming.json').read_text()))
    schema=load_root_schema()
    raw=IntegrationContext(before,incoming,schema)
    sources=tuple(dict.fromkeys([s for n in raw.raw_nodes.values() for s in
        (*n.sources,*(s for a in n.alternatives for s in a.sources))]+[s for e in raw.raw_edges.values() for s in e.sources]))
    resolver=SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(json.loads((root/'source_evidence.json').read_text())))
    context=IntegrationContext(before,incoming,schema,evidence=resolver.resolve(sources))
    context.sql_dialect='sqlite'
    assert input_fingerprint(context)==cp['input_fingerprint']
    engine=IncrementalGraphIntegrationEngine(SimpleNamespace(reconciliation_contract=True))
    engine.session.inputs=context;engine.session.current=GraphState.model_validate(cp['current'])
    engine.session.contract_versions=ContractVersions.from_dict(cp['contract_versions'])
    engine.session.signal_receipts=deepcopy(cp.get('signal_receipts',[]))
    engine.session.history=deepcopy(cp['history']);engine.session.lineage=LineageIndex.restore(context,cp['lineage'])
    engine.session.task_state=None;engine.session.relation_followups=[];engine.session.open_tasks=[]
    latest={row['check_key']:row for row in engine.session.signal_receipts}
    # Compare against the next ACTUAL detector definition. Replaying only the
    # stored trigger can miss a stale membership manifest after a valid repair.
    detected={c['id']:c for c in inspection_signals(context,Preview(engine.session.current,{}, {},[]))}
    checks=[];stale=[];changed=[];closed=[]
    for row in latest.values():
        original=row['check'];current=detected.get(original.get('id'))
        if original['kind'] in SIGNALS and current is not None:
            if is_accepted((engine).session,current):checks.append(current)
            elif is_accepted((engine).session,original):stale.append(original.get('id'))
            else:changed.append(original.get('id'))
        elif is_accepted((engine).session,original):
            checks.append(original);closed.append(original.get('id'))
    result={'audit_protocol':2,'checked_at':datetime.now(timezone.utc).isoformat(),'database':plan['database'],
        'batch':index,'accepted_receipts':len(engine.session.signal_receipts),'matching_current_checks':len(checks),
        'check_ids':[c.get('id') for c in checks],'snapshot_revision':cp['revision'],
        'live_checkpoint_mutated':False,'provider_calls':0,
        'stale_accepted_manifests':stale,'changed_premises_recheck_expected':changed,
        'closed_or_explicit_obligation_receipts':closed}
    if stale:
        result['status']='stale_accepted_manifest'
        return result
    if not checks:
        result['status']='awaiting_current_accepted_receipt'
        return result
    calls=[]
    def forbidden(task,*args,**kwargs):
        calls.append(task.id)
        raise AssertionError('Accepted checks unexpectedly re-entered model task: '+task.id)
    engine._run_task=forbidden
    import cortex.capabilities.graph_merge.engine as scheduler
    original_inspection,original_batches=scheduler.inspection_signals,scheduler.review_batches
    history=deepcopy(engine.session.history)
    try:
        scheduler.inspection_signals=lambda *args,**kwargs:deepcopy(checks)
        targets=set().union(*(_signal_targets(c,engine.session.current) for c in checks))
        for grouping in ('single_group','one_check_per_group'):
            scheduler.review_batches=(lambda current,checks,**kwargs:[
                (set().union(*(_signal_targets(c,current) for c in checks)),checks)] if checks else []) if grouping=='single_group' else (
                lambda current,checks,**kwargs:[(_signal_targets(c,current),[c]) for c in checks])
            engine._reconcile('read-only-receipt-audit',initial_targets=targets)
        assert engine.session.history==history and not calls
    finally:
        scheduler.inspection_signals,scheduler.review_batches=original_inspection,original_batches
    original=engine.session.current
    extra=GraphNode(id='receipt.audit.unrelated',node_type='concept',name='Read-only audit control',
        properties={'definition':'An isolated control outside every check.'})
    engine.session.current=original.model_copy(update={'graph':original.graph.model_copy(update={'nodes':(*original.graph.nodes,extra)})})
    assert all(is_accepted((engine).session,c) for c in checks)
    engine.session.current=original
    c=checks[0]
    anchors=_signal_targets(c,original)
    selected=next(n for n in original.graph.nodes if n.id in anchors and n.node_type not in {'table','column','dimension_value'})
    changed=selected.model_copy(update={'description':(selected.description or '')+' [Detached relevant-condition control] '})
    engine.session.current=original.model_copy(update={'graph':original.graph.model_copy(update={'nodes':tuple(
        changed if n.id==selected.id else n for n in original.graph.nodes)})})
    assert not is_accepted((engine).session,c)
    result.update(status='passed',regrouping_scheduler_task_calls=len(calls),
        accepted_history_unchanged=True,unrelated_change_reused=True,relevant_change_invalidated=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--batch',type=int)
    args=parser.parse_args()
    answer=audit(args.root,batch=args.batch)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(answer,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(answer,ensure_ascii=False))
