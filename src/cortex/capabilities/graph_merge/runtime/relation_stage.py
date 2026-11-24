"""Engine transaction boundary for the description-only relation merger."""
from copy import deepcopy

from cortex.capabilities.graph_merge.graph.batch import delta
from cortex.capabilities.graph_merge.graph.lineage import LineageIndex
from cortex.capabilities.graph_merge.runtime.state import canonical_hash


def run_relation_merger(engine, run_id):
    if not getattr(engine.agent, 'relation_merger_contract', False):
        return
    from cortex.capabilities.graph_merge.reconciliation.relation_merger import (
        consolidate_relations, relation_groups,
    )
    for _ in range(2):
        base = engine.session.current
        groups = relation_groups(base)
        cp = getattr(engine.session, 'relation_merger_checkpoint', {})
        if cp and canonical_hash(base) not in {cp.get('source_hash'), cp.get('state_hash')}:
            history = list(getattr(engine.session, 'relation_merger_history', ()))
            history.append({'reason': 'A new accepted graph requires a newly bound relation task', 'checkpoint': deepcopy(cp)})
            engine.session.relation_merger_history = history
            cp = engine.session.relation_merger_checkpoint = {}
            engine._persist(run_id)
        if not groups:
            return
        def save(record):
            # Completed service application is accepted only together with the
            # engine graph/lineage/history below. Earlier receipts are durable.
            if record.get('stage') != 'completed':
                engine.session.relation_merger_checkpoint = deepcopy(record)
                engine._persist(run_id)
        result = consolidate_relations(engine.agent, engine.session.inputs, base,
            checkpoint=cp or None, save=save,
            batch_size=getattr(engine.agent, 'relation_merger_batch_size',10))
        before = engine.session.acceptance_snapshot()
        try:
            lineage = LineageIndex.restore(engine.session.inputs, engine.session.lineage.snapshot())
            lineage.observe(base, result.state, edge_aliases=result.edge_aliases,
                explanation='Consolidate relation descriptions without independent Assessment; preserve exact sources and structural scope.')
            engine.session.current, engine.session.lineage = result.state, lineage
            engine.session.relation_merger_checkpoint = deepcopy(result.checkpoint)
            engine.session.revision += 1
            engine.session.history = [*engine.session.history, {'task_id': 'relation-merger-' + canonical_hash(base)[:20],
                'phase': 'relation_merge', 'revision': engine.session.revision,
                'assessment': 'not_required', 'delta': delta(base, result.state),
                'edge_aliases': result.edge_aliases}]
            engine._persist(run_id)
        except BaseException:
            engine.session.restore_acceptance(before)
            raise
        return
    raise ValueError('Relation merger coupled maintenance did not close; unpublished checkpoint retained')
