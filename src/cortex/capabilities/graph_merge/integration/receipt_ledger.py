"""Controller-owned accepted reconciliation receipts.

The ledger is an append-only audit trail, stored atomically with accepted graph
and history. Graph hashes bind the historical commit; only the per-check premise
and policy hashes decide reuse. A digest detects inconsistent records, not edits
made by an adversary able to rewrite the whole trusted checkpoint.
"""

import json
import re
from collections import Counter
from copy import deepcopy

from cortex.capabilities.graph_merge.integration.task_state import canonical_hash


class ReceiptValidationError(ValueError):
    """An accepted receipt is malformed or does not match its commit."""


RECEIPT_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = frozenset({
    'version', 'check_key', 'check', 'check_hash', 'premise_hash', 'policy_hash',
    'dependencies', 'task_id', 'accepted_revision', 'candidate_hash',
    'history_hash', 'record_hash',
})


def _json(value):
    """Detach JSON while rejecting NaN, non-string keys and custom objects."""
    def keys(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ReceiptValidationError('Receipt JSON object keys must be strings')
            for child in item.values():
                keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                keys(child)

    keys(value)
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ReceiptValidationError('Receipt must contain finite JSON data') from exc


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ReceiptValidationError(f'Receipt {label} must be a nonempty string')
    return value


def _hash(value, label):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ReceiptValidationError(f'Receipt {label} must be a SHA256 hex digest')
    return value


def _check(value):
    if not isinstance(value, dict):
        raise ReceiptValidationError('Receipt check must be an object')
    value = _json(value)
    _text(value.get('kind'), 'check kind')
    if 'id' in value:
        _text(value['id'], 'check ID')
    if 'signal_id' in value:
        _text(value['signal_id'], 'signal ID')
    from cortex.capabilities.graph_merge.integration.signal_premises import (
        normalize_signal,
    )
    return normalize_signal(value)


def check_key(check):
    """Semantic check identity, independent of task ID, batching and cycles.

    Named signals retain a stable key when their requirements change; check_hash
    then invalidates the old receipt. Anonymous obligations are content keyed.
    """
    check = _check(check)
    ident = check.get('id') or check.get('signal_id')
    identity = {'kind': check['kind'], 'id': ident} if ident is not None else check
    return canonical_hash(identity)


def _dependencies(value):
    if not isinstance(value, (list, tuple)):
        raise ReceiptValidationError('Receipt dependencies must be an array of query objects')
    rows = _json(list(value))
    if any(not isinstance(row, dict) or not row for row in rows):
        raise ReceiptValidationError('Receipt dependencies must contain nonempty query objects')
    hashes = [canonical_hash(row) for row in rows]
    if len(set(hashes)) != len(hashes):
        raise ReceiptValidationError('Receipt dependencies must not repeat a query')
    return rows


def _history_binding(entry, policy_hash, candidate_hash):
    if not isinstance(entry, dict):
        raise ReceiptValidationError('Receipt requires an accepted history object')
    if entry.get('phase') != 'reconcile' or entry.get('assessment') != 'graph_assess':
        raise ReceiptValidationError('Receipt requires independently accepted reconciliation history')
    _text(entry.get('task_id'), 'history task ID')
    revision = entry.get('revision')
    if type(revision) is not int or revision < 1:
        raise ReceiptValidationError('Receipt requires a positive accepted revision')
    if entry.get('candidate_hash') != candidate_hash:
        raise ReceiptValidationError('Receipt candidate does not match accepted history')
    if entry.get('reconciliation_receipt_policy_hash') != policy_hash:
        raise ReceiptValidationError('Receipt policy does not match accepted history')
    if 'reconciliation_check_hashes' in entry:
        if not isinstance(entry['reconciliation_check_hashes'], list):
            raise ReceiptValidationError('Accepted history check manifest must be an array')
        for item in entry['reconciliation_check_hashes']:
            _hash(item, 'history check manifest hash')


def make_receipt(check, *, premise_hash, policy_hash, history_entry,
                 candidate_hash, dependencies=()):
    """Create only after independent accept, inside the graph commit transaction."""
    check = _check(check)
    for value, name in ((premise_hash, 'premise hash'), (policy_hash, 'policy hash'),
                        (candidate_hash, 'candidate hash')):
        _hash(value, name)
    history_entry = _json(history_entry)
    _history_binding(history_entry, policy_hash, candidate_hash)
    if ('reconciliation_check_hashes' in history_entry
            and canonical_hash(check) not in history_entry['reconciliation_check_hashes']):
        raise ReceiptValidationError('Receipt check is absent from accepted history manifest')
    record = {
        'version': RECEIPT_VERSION, 'check_key': check_key(check), 'check': check,
        'check_hash': canonical_hash(check), 'premise_hash': premise_hash,
        'policy_hash': policy_hash, 'dependencies': _dependencies(dependencies),
        'task_id': history_entry['task_id'],
        'accepted_revision': history_entry['revision'],
        'candidate_hash': candidate_hash, 'history_hash': canonical_hash(history_entry),
    }
    record['record_hash'] = canonical_hash(record)
    return record


def validate_receipt_ledger(rows, *, history, current=None):
    """Validate persisted records and their exact accepted-commit bindings.

    Missing legacy fields must be handled explicitly by the caller; None is not
    an empty ledger. Old receipts may describe older accepted graphs. Only a
    receipt at the latest history revision can be compared with current.
    """
    if not isinstance(rows, list):
        raise ReceiptValidationError('Reconciliation receipt ledger must be an array')
    if not isinstance(history, (list, tuple)):
        raise ReceiptValidationError('Receipt history must be an array')
    rows = _json(rows)
    by_revision = {}
    for entry in history:
        if not isinstance(entry, dict):
            raise ReceiptValidationError('Receipt history entries must be objects')
        revision = entry.get('revision')
        if type(revision) is int:
            by_revision.setdefault(revision, []).append(entry)
    latest_revision = max(by_revision, default=0)
    if hasattr(current, 'model_dump'):
        current = current.model_dump(mode='json')
    current_hash = canonical_hash(current) if current is not None else None
    seen = set()
    seen_checks = set()
    actual_checks = {}
    last_revision = 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ReceiptValidationError('Receipt fields are incomplete or unknown')
        if type(row['version']) is not int or row['version'] != RECEIPT_VERSION:
            raise ReceiptValidationError('Unsupported reconciliation receipt version')
        for name in ('check_key', 'check_hash', 'premise_hash', 'policy_hash',
                     'candidate_hash', 'history_hash', 'record_hash'):
            _hash(row[name], name)
        check = _check(row['check'])
        if row['check_key'] != check_key(check) or row['check_hash'] != canonical_hash(check):
            raise ReceiptValidationError('Receipt check content or identity hash mismatch')
        _dependencies(row['dependencies'])
        if row['record_hash'] != canonical_hash({k:v for k,v in row.items() if k!='record_hash'}):
            raise ReceiptValidationError('Receipt record hash mismatch')
        if row['record_hash'] in seen:
            raise ReceiptValidationError('Duplicate reconciliation receipt record')
        seen.add(row['record_hash'])
        revision = row['accepted_revision']
        if type(revision) is not int or revision < 1 or revision < last_revision:
            raise ReceiptValidationError('Receipt revisions must be positive and append ordered')
        last_revision = revision
        identity = (revision, row['check_key'])
        if identity in seen_checks:
            raise ReceiptValidationError('Duplicate receipt check in one accepted revision')
        seen_checks.add(identity)
        actual_checks.setdefault(revision, []).append(row['check_hash'])
        matches = by_revision.get(revision, [])
        if len(matches) != 1:
            raise ReceiptValidationError('Receipt has no unique accepted history revision')
        entry = matches[0]
        _history_binding(entry, row['policy_hash'], row['candidate_hash'])
        if row['task_id'] != entry['task_id'] or row['history_hash'] != canonical_hash(entry):
            raise ReceiptValidationError('Receipt accepted history binding mismatch')
        if revision == latest_revision and current_hash is not None and row['candidate_hash'] != current_hash:
            raise ReceiptValidationError('Latest receipt does not describe the accepted current snapshot')
    for revision, entries in by_revision.items():
        for entry in entries:
            if 'reconciliation_check_hashes' not in entry:
                continue
            expected = entry['reconciliation_check_hashes']
            if not isinstance(expected, list):
                raise ReceiptValidationError('Accepted history check manifest must be an array')
            for item in expected:
                _hash(item, 'history check manifest hash')
            if len(entries) != 1 or Counter(actual_checks.get(revision, [])) != Counter(expected):
                raise ReceiptValidationError('Receipt ledger does not match accepted history check manifest')
    return rows


def latest_receipt(rows, check, *, policy_hash):
    """Return the latest record for this semantic key under the exact policy."""
    _hash(policy_hash, 'policy hash')
    key = check_key(check)
    for row in reversed(rows):
        if row['check_key'] == key and row['policy_hash'] == policy_hash:
            return deepcopy(row)
    return None


def has_receipt(rows, check, *, premise_hash, policy_hash):
    """Reuse only the latest accepted decision with unchanged relevant facts."""
    _hash(premise_hash, 'premise hash')
    row = latest_receipt(rows, check, policy_hash=policy_hash)
    return row is not None and row['check_hash'] == canonical_hash(_check(check)) and row['premise_hash'] == premise_hash
