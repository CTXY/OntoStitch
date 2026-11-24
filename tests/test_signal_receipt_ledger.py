"""Strict accepted-receipt audit bindings and latest-decision lookup."""
from copy import deepcopy

import pytest

from cortex.capabilities.graph_merge.integration.receipt_ledger import (
    check_key,
    has_receipt,
    latest_receipt,
    make_receipt,
    validate_receipt_ledger,
)
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash

CHECK = {'id': 'source_selection_variants:bond', 'kind': 'source_selection_variants',
         'node_ids': ['bond'], 'required_outcome': 'Preserve compatible matching roles'}
POLICY = canonical_hash('receipt-policy-1')
PREMISE = canonical_hash('bond-content-and-relevant-relations')
CURRENT = {'graph': {'nodes': [], 'edges': []}}


def fixture(revision=1, current=None, premise_hash=PREMISE, check=None):
    current = CURRENT if current is None else current
    entry = {'task_id': 'structure-' + str(revision), 'phase': 'reconcile',
             'revision': revision, 'assessment': 'graph_assess',
             'candidate_hash': canonical_hash(current),
             'reconciliation_receipt_policy_hash': POLICY,
             'delta': {'nodes': {'updated': []}, 'edges': {'updated': []}}}
    receipt = make_receipt(check or CHECK, premise_hash=premise_hash, policy_hash=POLICY,
                           history_entry=entry, candidate_hash=canonical_hash(current),
                           dependencies=[{'tool': 'read_evidence', 'ids': ['e1']}])
    return receipt, entry


def reseal(row):
    row['record_hash'] = canonical_hash({k:v for k,v in row.items() if k!='record_hash'})
    return row


def test_accepted_noop_receipt_roundtrip_and_lookup():
    receipt, entry = fixture()
    loaded = validate_receipt_ledger([receipt], history=[entry], current=CURRENT)
    assert loaded == [receipt] and loaded[0] is not receipt
    assert has_receipt(loaded, CHECK, premise_hash=PREMISE, policy_hash=POLICY)
    assert not has_receipt(loaded, CHECK, premise_hash=canonical_hash('new fact'), policy_hash=POLICY)
    assert not has_receipt(loaded, CHECK, premise_hash=PREMISE, policy_hash=canonical_hash('new policy'))


def test_same_signal_id_with_changed_required_outcome_invalidates():
    receipt, _ = fixture()
    changed = {**CHECK, 'required_outcome': 'Preserve a newly required consumer'}
    assert check_key(changed) == check_key(CHECK)
    assert not has_receipt([receipt], changed, premise_hash=PREMISE, policy_hash=POLICY)


def test_reordered_reference_sets_hit_without_reordering_business_sequences():
    check = {**CHECK, 'node_ids': ['bond','atom'], 'edge_ids': ['e2','e1'],
             'targets': [['physical1'], ['physical2']], 'actions': ['first','second']}
    receipt, _ = fixture(check=check)
    reordered = {**check, 'node_ids': ['atom','bond'], 'edge_ids': ['e1','e2']}
    assert has_receipt([receipt], reordered, premise_hash=PREMISE, policy_hash=POLICY)
    assert not has_receipt([receipt], {**reordered, 'targets': list(reversed(check['targets']))},
                           premise_hash=PREMISE, policy_hash=POLICY)
    assert not has_receipt([receipt], {**reordered, 'actions': list(reversed(check['actions']))},
                           premise_hash=PREMISE, policy_hash=POLICY)


def test_latest_record_is_authoritative_and_old_premise_cannot_bypass_it():
    old, old_entry = fixture()
    new, new_entry = fixture(2, premise_hash=canonical_hash('new fact'))
    ledger = validate_receipt_ledger([old, new], history=[old_entry,new_entry], current=CURRENT)
    assert not has_receipt(ledger, CHECK, premise_hash=PREMISE, policy_hash=POLICY)
    assert has_receipt(ledger, CHECK, premise_hash=new['premise_hash'], policy_hash=POLICY)
    found = latest_receipt(ledger, CHECK, policy_hash=POLICY)
    found['check']['node_ids'].clear()
    assert ledger[-1]['check']['node_ids'] == ['bond']


def test_historical_receipt_does_not_require_current_global_graph_hash():
    receipt, entry = fixture()
    changed = {'graph': {'nodes': [{'id': 'unrelated'}], 'edges': []}}
    later = {'task_id': 'another-task', 'revision': 2, 'candidate_hash': canonical_hash(changed)}
    assert validate_receipt_ledger([receipt], history=[entry,later], current=changed)
    assert has_receipt([receipt], CHECK, premise_hash=PREMISE, policy_hash=POLICY)
    with pytest.raises(ValueError, match='current snapshot'):
        validate_receipt_ledger([receipt], history=[entry], current=changed)


@pytest.mark.parametrize('bad', [None, {}, (), 'empty', True])
def test_missing_or_malformed_ledger_is_not_silently_fresh(bad):
    with pytest.raises(ValueError, match='ledger must be an array'):
        validate_receipt_ledger(bad, history=[])


@pytest.mark.parametrize('field', ['record_hash', 'policy_hash', 'premise_hash', 'check',
                                 'dependencies', 'accepted_revision', 'history_hash'])
def test_incomplete_receipt_fails_closed(field):
    row, entry = fixture()
    del row[field]
    with pytest.raises(ValueError, match='fields'):
        validate_receipt_ledger([row], history=[entry])


@pytest.mark.parametrize('value', [True, 2, None, '1'])
def test_unknown_or_coerced_version_rejected(value):
    row, entry = fixture()
    row['version'] = value
    reseal(row)
    with pytest.raises(ValueError, match='version'):
        validate_receipt_ledger([row], history=[entry])


def test_unsealed_and_resealed_forgery_fail_at_their_bindings():
    row, entry = fixture()
    bad = deepcopy(row)
    bad['premise_hash'] = canonical_hash('invented premise')
    with pytest.raises(ValueError, match='record hash'):
        validate_receipt_ledger([bad], history=[entry])
    bad = reseal({**row, 'candidate_hash': canonical_hash('invented graph')})
    with pytest.raises(ValueError, match='candidate'):
        validate_receipt_ledger([bad], history=[entry])
    bad = reseal({**row, 'policy_hash': canonical_hash('invented policy')})
    with pytest.raises(ValueError, match='policy'):
        validate_receipt_ledger([bad], history=[entry])
    bad = deepcopy(row)
    bad['check']['required_outcome'] = 'Invented outcome'
    reseal(bad)
    with pytest.raises(ValueError, match='check content'):
        validate_receipt_ledger([bad], history=[entry])


def test_unknown_duplicate_and_modified_accepted_history_rejected():
    row, entry = fixture()
    with pytest.raises(ValueError, match='unique accepted history'):
        validate_receipt_ledger([row], history=[])
    with pytest.raises(ValueError, match='unique accepted history'):
        validate_receipt_ledger([row], history=[entry, deepcopy(entry)])
    altered = {**entry, 'task_id': 'unaccepted-task'}
    with pytest.raises(ValueError, match='history binding'):
        validate_receipt_ledger([row], history=[altered])


def test_ledger_append_order_and_identical_record_duplicates_rejected():
    old, old_entry = fixture()
    new, new_entry = fixture(2)
    with pytest.raises(ValueError, match='append ordered'):
        validate_receipt_ledger([new,old], history=[old_entry,new_entry])
    with pytest.raises(ValueError, match='Duplicate'):
        validate_receipt_ledger([old,deepcopy(old)], history=[old_entry])


def test_anonymous_obligations_are_content_keyed_without_scheduler_ids():
    obligation = {'kind': 'node_relation_followup', 'node_ids': ['bond'],
                  'required_outcome': 'Retain supported endpoint relations'}
    assert check_key(obligation) == check_key(deepcopy(obligation))
    assert check_key({**obligation,'required_outcome':'Preserve another fact'}) != check_key(obligation)


@pytest.mark.parametrize('dependencies', [None, {}, [None], [{}], [{'id':'e1'}, {'id':'e1'}]])
def test_dependency_queries_are_explicit_unique_objects(dependencies):
    _, entry = fixture()
    with pytest.raises(ValueError, match='dependencies'):
        make_receipt(CHECK, premise_hash=PREMISE, policy_hash=POLICY, history_entry=entry,
                     candidate_hash=canonical_hash(CURRENT), dependencies=dependencies)


def test_make_receipt_rejects_nonacceptance_and_nonfinite_data():
    _, entry = fixture()
    for modified in ({**entry,'assessment':'skipped_no_change'}, {**entry,'phase':'consolidate'},
                     {**entry,'revision':True}):
        with pytest.raises(ValueError):
            make_receipt(CHECK,premise_hash=PREMISE,policy_hash=POLICY,
                         history_entry=modified,candidate_hash=canonical_hash(CURRENT))
    with pytest.raises(ValueError, match='finite JSON'):
        check_key({**CHECK,'unsupported':float('nan')})
    with pytest.raises(ValueError, match='keys must be strings'):
        check_key({**CHECK,'unsupported':{1:'value'}})


def test_failed_validation_does_not_mutate_prior_ledger_or_history():
    row, entry = fixture()
    ledger, history = [row], [entry]
    before = deepcopy((ledger,history))
    invalid = reseal({**row,'accepted_revision':2})
    with pytest.raises(ValueError):
        validate_receipt_ledger([*ledger, invalid],history=history,current=CURRENT)
    assert (ledger,history) == before


def test_history_manifest_requires_each_accepted_check_receipt_exactly_once():
    first_check = CHECK
    second_check = {**CHECK, 'id': 'source_selection_variants:atom', 'node_ids': ['atom']}
    _, entry = fixture()
    entry['reconciliation_check_hashes'] = [canonical_hash(first_check), canonical_hash(second_check)]
    rows = [make_receipt(check, premise_hash=PREMISE, policy_hash=POLICY,
                         history_entry=entry, candidate_hash=canonical_hash(CURRENT))
            for check in (first_check, second_check)]
    assert validate_receipt_ledger(rows, history=[entry], current=CURRENT) == rows
    for missing in ([], rows[:1], rows[1:]):
        with pytest.raises(ValueError, match='history check manifest'):
            validate_receipt_ledger(missing, history=[entry], current=CURRENT)
    extra = deepcopy(rows[0]);extra['premise_hash'] = canonical_hash('different premise');reseal(extra)
    with pytest.raises(ValueError, match='Duplicate receipt check'):
        validate_receipt_ledger([*rows,extra], history=[entry])


@pytest.mark.parametrize('manifest',[None,{},['bad-hash'],[canonical_hash('unaccepted check')]])
def test_invalid_or_mismatching_history_manifests_rejected(manifest):
    row, entry = fixture()
    entry['reconciliation_check_hashes'] = manifest
    row['history_hash'] = canonical_hash(entry);reseal(row)
    with pytest.raises(ValueError):
        validate_receipt_ledger([row], history=[entry])
