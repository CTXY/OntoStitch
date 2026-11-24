"""Attach reviewed scope-narrowing proofs without changing accepted graph/history."""
import argparse
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.reconciliation.scope_migration import (
    build_scope_migration,
    validate_scope_migrations,
)
from cortex.capabilities.graph_merge.runtime.state import (
    ContractVersions,
    build_contract_versions,
    canonical_hash,
)
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.capabilities.sql_training.graph_merge_evidence import (
    MappingEvidenceRepository,
    SqlTrainingGraphMergeEvidenceResolver,
)
from cortex.schema import GraphState, load_root_schema


def read(path):
    return json.loads(path.read_text())


def attach(root, audit_file, transition_file, *, dry_run=False):
    plan = read(root / 'plan.json')
    status = read(root / 'status.json')
    if status['status'] not in {'stopped', 'needs_attention', 'ready', 'prepared'}:
        raise ValueError('Attach compatibility proofs before launching the migrated run')
    audit, transition = read(audit_file), read(transition_file)
    unfinished = [int(p.name.split('-')[-1]) for p in (root / 'merge').glob('batch-*')
                  if (p / 'workspace.sqlite').exists() and not (p / 'completed.json').exists()]
    if len(unfinished) != 1:
        raise ValueError('Receipt attachment requires exactly one unfinished batch')
    batch = unfinished[0]
    folder = root / 'merge' / f'batch-{batch:04d}'
    run_id = f'continue-{plan["database"]}-batch-{batch:04d}'
    store = WorkspaceStore(folder / 'workspace.sqlite')
    payload = store.load(run_id)
    current = GraphState.model_validate(payload['current'])
    if canonical_hash(current) != audit['source_current_hash']:
        raise ValueError('Frozen old audit does not describe this migrated accepted graph')
    before = GraphState.model_validate(read(folder / 'before.json'))
    incoming = GraphState.model_validate(read(folder / 'incoming.json'))
    schema = load_root_schema()
    raw = IntegrationContext(before, incoming, schema)
    sources = tuple(dict.fromkeys([s for n in raw.raw_nodes.values() for s in
        (*n.sources, *n.common_sources, *(s for a in n.alternatives for s in a.sources))]
        + [s for e in raw.raw_edges.values() for s in e.sources]))
    resolver = SqlTrainingGraphMergeEvidenceResolver(MappingEvidenceRepository(read(root / 'source_evidence.json')))
    ctx = IntegrationContext(before, incoming, schema, evidence=resolver.resolve(sources))
    ctx.sql_dialect = 'sqlite'
    engine = SimpleNamespace(ctx=ctx, current=current, signal_receipts=payload['signal_receipts'],
                             contract_versions=(build_contract_versions(schema) if dry_run else ContractVersions.from_dict(payload['contract_versions'])))
    proofs = deepcopy(payload.get('signal_scope_migrations', []))
    records = {r['record_hash']: r for r in payload['signal_receipts']}
    skipped = []
    for row in audit['records']:
        receipt = records[row['receipt_record_hash']]
        old = {'current_hash': audit['source_current_hash'], 'policy_hash': receipt['policy_hash'],
               'receipt_record_hash': receipt['record_hash'], 'premise': row['facts'],
               'source_contract_versions': audit['source_contract_versions']}
        try:
            proof = build_scope_migration(engine, receipt, old, source_policy_hash=transition['source_policy_hash'],
                                          reviewed_transition=transition)
            if proof not in proofs:
                proofs.append(proof)
        except ValueError as error:
            skipped.append({'check_id': receipt['check'].get('id'), 'reason': str(error)})
    validate_scope_migrations(proofs, ledger=payload['signal_receipts'])
    if not dry_run:
        old_graph, old_history = canonical_hash(payload['current']), canonical_hash(payload['history'])
        payload['signal_scope_migrations'] = proofs
        store.save(run_id, payload, expected_revision=payload['revision'])
        saved = store.load(run_id)
        assert canonical_hash(saved['current']) == old_graph and canonical_hash(saved['history']) == old_history
        assert saved['signal_receipts'] == payload['signal_receipts']
    result = {'run_id': run_id, 'proofs': len(proofs), 'skipped': skipped,
              'original_receipts_unchanged': True, 'accepted_graph_and_history_unchanged': True,
              'new_assessments_invented': 0, 'transition_hash': canonical_hash(transition)}
    if not dry_run:
        (root / 'scope_receipt_migration.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--transition', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(attach(args.root.resolve(), args.audit.resolve(), args.transition.resolve(), dry_run=args.dry_run), ensure_ascii=False))
