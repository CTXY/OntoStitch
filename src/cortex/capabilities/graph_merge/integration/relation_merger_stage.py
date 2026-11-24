"""Engine transaction boundary for the description-only relation merger."""
from copy import deepcopy

from cortex.capabilities.graph_merge.integration.batch import delta
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash


def run_relation_merger(engine, run_id):
    if not getattr(engine.agent, 'relation_merger_contract', False):
        return
    from cortex.capabilities.graph_merge.integration.relation_merger import (
        consolidate_relations, relation_groups,
    )
    for _ in range(2):
        base = engine.current
        groups = relation_groups(base)
        cp = getattr(engine, 'relation_merger_checkpoint', {})
        if cp and canonical_hash(base) not in {cp.get('source_hash'), cp.get('state_hash')}:
            history = list(getattr(engine, 'relation_merger_history', ()))
            history.append({'reason': 'A new accepted graph requires a newly bound relation task', 'checkpoint': deepcopy(cp)})
            engine.relation_merger_history = history
            cp = engine.relation_merger_checkpoint = {}
            engine._persist(run_id)
        if not groups:
            return
        def save(record):
            # Completed service application is accepted only together with the
            # engine graph/lineage/history below. Earlier receipts are durable.
            if record.get('stage') != 'completed':
                engine.relation_merger_checkpoint = deepcopy(record)
                engine._persist(run_id)
        result = consolidate_relations(engine.agent, engine.ctx, base,
            checkpoint=cp or None, save=save,
            batch_size=getattr(engine.agent, 'relation_merger_batch_size',10))
        before = {key: getattr(engine, key, None) for key in
            ('current', 'lineage', 'history', '_revision', '_storage_revision', 'relation_merger_checkpoint')}
        try:
            lineage = LineageIndex.restore(engine.ctx, engine.lineage.snapshot())
            lineage.observe(base, result.state, edge_aliases=result.edge_aliases,
                explanation='Consolidate relation descriptions without independent Assessment; preserve exact sources and structural scope.')
            engine.current, engine.lineage = result.state, lineage
            engine.relation_merger_checkpoint = deepcopy(result.checkpoint)
            engine._revision += 1
            engine.history = [*engine.history, {'task_id': 'relation-merger-' + canonical_hash(base)[:20],
                'phase': 'relation_merge', 'revision': engine._revision,
                'assessment': 'not_required', 'delta': delta(base, result.state),
                'edge_aliases': result.edge_aliases}]
            engine._persist(run_id)
        except BaseException:
            for key, value in before.items():
                setattr(engine, key, value)
            raise
        return
    raise ValueError('Relation merger coupled maintenance did not close; unpublished checkpoint retained')
