"""Scope-only compatibility preserves old accepts without inventing new reviews."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_signal_premises import add, check, edge, node, setup

from cortex.capabilities.graph_merge.integration.receipt_ledger import (
    make_receipt,
    validate_receipt_ledger,
)
from cortex.capabilities.graph_merge.integration.signal_premises import (
    normalize_signal,
    premise_object,
    signal_premise,
)
from cortex.capabilities.graph_merge.integration.signal_receipts import (
    dependency_facts,
    policy_hash,
)
from cortex.capabilities.graph_merge.integration.signal_scope_migration import (
    _subset_rows,
    build_scope_migration,
    migrated_receipt,
    validate_scope_migrations,
)
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash


def transition(engine, receipt):
    relative = 'src/cortex/capabilities/graph_merge/integration/signal_scope.py'
    target = Path(__file__).resolve().parents[1] / relative
    return {'source_policy_hash': receipt['policy_hash'], 'target_policy_hash': policy_hash(engine),
            'files': {relative: {'before': None, 'after': sha256(target.read_bytes()).hexdigest()}}}


def migration_case():
    ctx, state = setup([node('A'), node('B'), node('C'), node('D')], [
        edge('AB', 'A', 'B'), edge('BC', 'B', 'C'), edge('AC', 'A', 'C'), edge('Noise', 'A', 'D')])
    signal = normalize_signal(check('triangle', node_ids=['A', 'B', 'C'], edge_ids=['AB', 'BC', 'AC']))
    engine = SimpleNamespace(ctx=ctx, current=state, signal_receipts=[], signal_scope_migrations=[],
                             contract_versions=SimpleNamespace(to_dict=lambda: {'schema': 'same-schema', 'review_policy': 'same-review-policy',
                                 'task_state': 4, 'tools': 'v2', 'prompts': 'v2'}))
    # The old frozen exporter supplies a broader, complete authoritative view.
    old_check = deepcopy(signal_premise(ctx, state, signal))
    old_check['version'] = 1
    old_check['nodes'] = [premise_object(n) for n in state.graph.nodes]
    old_check['edges'] = [premise_object(e) for e in state.graph.edges]
    old = {'check': old_check, 'supplementary': []}
    old_versions = {'schema': 'same-schema', 'review_policy': 'same-review-policy', 'task_state': 4,
                    'tools': 'v1', 'prompts': 'v1'}
    source_policy = canonical_hash({'receipt_protocol': 1, 'contracts': old_versions})
    history = {'task_id': 'original-accepted-task', 'phase': 'reconcile', 'assessment': 'graph_assess',
               'revision': 1, 'candidate_hash': canonical_hash(state),
               'reconciliation_receipt_policy_hash': source_policy,
               'reconciliation_check_hashes': [canonical_hash(signal)]}
    receipt = make_receipt(signal, premise_hash=canonical_hash(old), policy_hash=source_policy,
                           history_entry=history, candidate_hash=canonical_hash(state))
    engine.signal_receipts = validate_receipt_ledger([receipt], history=[history], current=state)
    audit = {'current_hash': canonical_hash(state), 'policy_hash': source_policy, 'source_contract_versions': old_versions,
             'receipt_record_hash': receipt['record_hash'], 'premise': old}
    return engine, receipt, audit, history


def test_scope_narrowing_retains_immutable_original_accept_and_roundtrips_proof():
    engine, receipt, audit, history = migration_case()
    ledger_before, history_before = deepcopy(engine.signal_receipts), deepcopy(history)
    proof = build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))
    engine.signal_scope_migrations = validate_scope_migrations([proof], ledger=engine.signal_receipts)
    assert migrated_receipt(engine, receipt['check']) == receipt
    assert engine.signal_receipts == ledger_before and history == history_before
    assert proof['subset_counts']['edges'] == {'source': 4, 'target': 3}
    assert proof['subset_counts']['nodes'] == {'source': 4, 'target': 3}
    restarted = SimpleNamespace(**{**engine.__dict__, 'signal_scope_migrations': deepcopy([proof])})
    assert migrated_receipt(restarted, receipt['check']) == receipt


def test_unrelated_changed_adjacency_reuses_but_related_change_and_new_pair_do_not():
    engine, receipt, audit, _ = migration_case()
    engine.signal_scope_migrations = [build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))]
    original = engine.current
    engine.current = add(original, nodes=[node('Extra')], edges=[edge('Noise2', 'A', 'Extra')])
    assert migrated_receipt(engine, receipt['check']) == receipt
    engine.current = add(original, edges=[edge('Relevant', 'A', 'B')])
    assert migrated_receipt(engine, receipt['check']) is None
    engine.current = original.model_copy(update={'graph': original.graph.model_copy(update={'nodes': tuple(
        n.model_copy(update={'description': 'Changed actual meaning'}) if n.id == 'A' else n
        for n in original.graph.nodes)})})
    assert migrated_receipt(engine, receipt['check']) is None


@pytest.mark.parametrize('field', ['current_hash', 'policy_hash', 'receipt_record_hash'])
def test_audit_must_bind_exact_current_snapshot_receipt_and_reviewed_source_policy(field):
    engine, receipt, audit, _ = migration_case()
    audit[field] = '2' * 64
    with pytest.raises(ValueError, match='not bound'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))


def test_old_valid_boolean_cannot_substitute_for_recomputed_facts():
    engine, receipt, audit, _ = migration_case()
    del audit['premise']
    audit['old_valid'] = True
    with pytest.raises(ValueError, match='complete recomputed'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))


def test_stale_old_receipt_cannot_be_migrated_even_if_new_narrower_scope_is_unchanged():
    engine, receipt, audit, _ = migration_case()
    audit['premise']['check']['nodes'][-1]['description'] = 'Old broader fact changed'
    with pytest.raises(ValueError, match='no longer matches'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))


def test_valid_old_accept_with_different_new_fact_is_not_a_subset():
    engine, receipt, audit, _ = migration_case()
    audit['premise']['check']['nodes'][0]['description'] = 'Different from candidate current'
    receipt['premise_hash'] = canonical_hash(audit['premise'])
    # Rehashing the test receipt simulates another independently accepted source
    # record; subset checking still rejects the actual current fact difference.
    receipt['record_hash'] = canonical_hash({k: v for k, v in receipt.items() if k != 'record_hash'})
    engine.signal_receipts = [receipt]
    audit['receipt_record_hash'] = receipt['record_hash']
    with pytest.raises(ValueError, match='exact fact subset'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))


def test_new_native_accept_takes_precedence_over_migration_even_after_facts_change():
    engine, receipt, audit, _ = migration_case()
    proof = build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))
    engine.signal_scope_migrations = [proof]
    native = deepcopy(receipt)
    native['policy_hash'] = policy_hash(engine)
    engine.signal_receipts.append(native)
    assert migrated_receipt(engine, receipt['check']) is None


def test_tampered_duplicate_or_unbound_attachment_is_rejected():
    engine, receipt, audit, _ = migration_case()
    proof = build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))
    with pytest.raises(ValueError, match='Duplicate'):
        validate_scope_migrations([proof, proof], ledger=engine.signal_receipts)
    altered = {**proof, 'target_premise_hash': '2' * 64}
    with pytest.raises(ValueError, match='integrity'):
        validate_scope_migrations([altered], ledger=engine.signal_receipts)
    with pytest.raises(ValueError, match='original accepted binding'):
        validate_scope_migrations([proof], ledger=[])


def test_dependencies_remain_conservative_after_narrowing():
    engine, receipt, audit, _ = migration_case()
    deps = [{'name': 'read_nodes', 'args': {'ids': ['D'], 'view': 'before'}}]
    receipt['dependencies'] = deps
    audit['premise']['supplementary'] = dependency_facts(engine.ctx, engine.current, deps)
    receipt['premise_hash'] = canonical_hash(audit['premise'])
    receipt['record_hash'] = canonical_hash({k: v for k, v in receipt.items() if k != 'record_hash'})
    engine.signal_receipts = [receipt]
    audit['receipt_record_hash'] = receipt['record_hash']
    engine.signal_scope_migrations = [build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=transition(engine, receipt))]
    original = engine.current
    engine.current = original.model_copy(update={'graph': original.graph.model_copy(update={'nodes': tuple(
        n.model_copy(update={'description': 'Changed explicit historical read'}) if n.id == 'D' else n
        for n in original.graph.nodes)})})
    assert migrated_receipt(engine, receipt['check']) is None


@pytest.mark.parametrize('field,value', [('schema', 'new-schema'), ('review_policy', 'new-review-policy'), ('task_state', 99)])
def test_scope_compatibility_cannot_cross_changed_business_or_assessment_contract(field, value):
    engine, receipt, audit, _ = migration_case()
    versions = {**engine.contract_versions.to_dict(), field: value}
    engine.contract_versions = SimpleNamespace(to_dict=lambda: versions)
    with pytest.raises(ValueError, match='semantic contract'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'],
                               reviewed_transition=transition(engine, receipt))


def test_transition_requires_actual_reviewed_target_file_hash_and_binds_source_versions():
    engine, receipt, audit, _ = migration_case()
    manifest = transition(engine, receipt)
    next(iter(manifest['files'].values()))['after'] = '0' * 64
    with pytest.raises(ValueError, match='changed after transition review'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'], reviewed_transition=manifest)
    audit['source_contract_versions']['tools'] = 'not-the-frozen-source'
    with pytest.raises(ValueError, match='source contract versions'):
        build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'],
                               reviewed_transition=transition(engine, receipt))


def test_restored_proof_rejects_unknown_version_and_invalid_subset_counts():
    engine, receipt, audit, _ = migration_case()
    proof = build_scope_migration(engine, receipt, audit, source_policy_hash=receipt['policy_hash'],
                                   reviewed_transition=transition(engine, receipt))
    for version in (True, 2):
        with pytest.raises(ValueError, match='fields or version'):
            validate_scope_migrations([{**proof, 'version': version}], ledger=engine.signal_receipts)
    altered = deepcopy(proof)
    altered['subset_counts']['edges']['target'] = 100
    altered['record_hash'] = canonical_hash({k: v for k, v in altered.items() if k != 'record_hash'})
    with pytest.raises(ValueError, match='invalid subset counts'):
        validate_scope_migrations([altered], ledger=engine.signal_receipts)



def test_evidence_subset_preserves_duplicate_handle_distinct_source_bindings():
    first = {'id': 'case42', 'source': {'source_id': 'case42', 'asset_id': 'concept.A'},
             'content': {'question': 'A precise original question', 'sql': 'SELECT A'}}
    second = {**deepcopy(first), 'source': {'source_id': 'case42', 'asset_id': 'edge.AB'}}
    assert _subset_rows([first, second], [second, first], 'evidence') == {'source': 2, 'target': 2}
    assert _subset_rows([first, second], [second], 'evidence') == {'source': 2, 'target': 1}
    # Both attribution and content are essential facts despite identical IDs.
    wrong_source = {**second, 'source': {'source_id': 'case42', 'asset_id': 'invented'}}
    wrong_content = {**second, 'content': {**second['content'], 'sql': 'SELECT DIFFERENT'}}
    for altered in (wrong_source, wrong_content):
        with pytest.raises(ValueError, match='exact fact subset: evidence'):
            _subset_rows([first, second], [first, altered], 'evidence')
    # Equal rows are a multiset too: one occurrence cannot prove two bindings.
    with pytest.raises(ValueError, match='exact fact subset: evidence'):
        _subset_rows([first, second], [first, first, second], 'evidence')
    assert _subset_rows([first, first, second], [first, first, second], 'evidence') == {'source': 3, 'target': 3}


def test_identical_v2_premises_reuse_across_unrelated_node_contract_update():
    engine,receipt,audit,_=migration_case()
    old=deepcopy(signal_premise(engine.ctx,engine.current,receipt['check']))
    audit['premise']['check']=old
    receipt['premise_hash']=canonical_hash(audit['premise'])
    receipt['record_hash']=canonical_hash({k:v for k,v in receipt.items() if k!='record_hash'})
    engine.signal_receipts=[receipt];audit['receipt_record_hash']=receipt['record_hash']
    proof=build_scope_migration(engine,receipt,audit,source_policy_hash=receipt['policy_hash'],reviewed_transition=transition(engine,receipt))
    engine.signal_scope_migrations=[proof]
    assert migrated_receipt(engine,receipt['check'])==receipt
    audit['premise']['check']['nodes'][0]['description']='A changed premise'
    receipt['premise_hash']=canonical_hash(audit['premise']);receipt['record_hash']=canonical_hash({k:v for k,v in receipt.items() if k!='record_hash'})
    engine.signal_receipts=[receipt];audit['receipt_record_hash']=receipt['record_hash']
    with pytest.raises(ValueError,match='exactly unchanged'):
        build_scope_migration(engine,receipt,audit,source_policy_hash=receipt['policy_hash'],reviewed_transition=transition(engine,receipt))
