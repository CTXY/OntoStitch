"""Persisted node-to-reconciliation obligations over stable original edge IDs."""
from copy import deepcopy

from cortex.capabilities.graph_merge.runtime.errors import IntegrationIncomplete
from cortex.capabilities.graph_merge.consolidation.relations import resolve_alias
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.consolidation.contract import RelationFollowup


def validate_followup_queue(rows):
    if not isinstance(rows, list):
        raise ValueError('Relation followup queue must be a list')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get('id') or row['id'] in seen:
            raise ValueError('Relation followups require unique stable IDs')
        seen.add(row['id'])
        if row['id'] != canonical_hash({key: value for key, value in row.items() if key != 'id'}):
            raise ValueError('Relation followup integrity hash mismatch')
        RelationFollowup.model_validate({key: row[key] for key in ('edge_ids', 'problem', 'required_outcome')})
        if not row.get('owner_ids') or not row.get('task_id') or not row.get('draft_id') or not row.get('candidate_hash'):
            raise ValueError('Relation followup lacks node task/candidate binding')
    return deepcopy(rows)


def drain_relation_followups(engine, run_id):
    """Require an independently assessed relation transaction per obligation.

    A committed history entry is the recovery proof: a crash between its commit
    and queue removal does not replay either Writer or Assessment.
    """
    rows = validate_followup_queue(list(getattr(engine, 'relation_followups', ())))
    for row in rows:
        task_id = 'node-followup-' + row['id'][:20]
        if not any(item['task_id'] == task_id and item['phase'] == 'reconcile' for item in engine.history):
            nodes = {node.id for node in engine.current.graph.nodes}
            edges = {edge.id: edge for edge in engine.current.graph.edges}
            owners = {resolve_alias(ident, engine.lineage.node_map) for ident in row['owner_ids']}
            owners &= nodes
            mapping = {}
            for original in row['edge_ids']:
                current = resolve_alias(row.get('edge_aliases', {}).get(original, original), engine.lineage.edge_map)
                mapping[original] = current if current in edges else None
            selected = {ident for ident in mapping.values() if ident is not None}
            targets = owners | selected
            targets |= {endpoint for ident in selected for endpoint in (edges[ident].from_node_id, edges[ident].to_node_id)}
            if not targets:
                raise IntegrationIncomplete('Relation followup has no live owner or relation; original records retained')
            feedback = {'kind': 'node_relation_followup',
                'node_ids': sorted(owners), 'edge_ids': sorted(selected), 'problem': row['problem'],
                'required_outcome': row['required_outcome']}
            check = {'id': 'followup:' + row['id'], 'kind': 'node_relation_followup',
                'mode': 'required', 'node_ids': sorted(owners), 'edge_ids': sorted(selected),
                'observed': feedback}
            engine._run_task(LocalTask(task_id, 'reconcile', targets, checks=[check], feedback=[feedback]), run_id)
        engine.relation_followups = [item for item in engine.relation_followups if item['id'] != row['id']]
        engine._persist(run_id)
