"""Audited reuse of an existing accept after a scope-only narrowing.

These attachments are compatibility proofs, never new Assessment decisions.
The original accepted ledger/history are immutable. Proof construction requires
facts recomputed by the frozen source implementation on the exact migration
snapshot; an ``old_valid`` boolean or an historical digest alone is insufficient.
"""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from cortex.capabilities.graph_merge.reconciliation.ledger import latest_receipt
from cortex.capabilities.graph_merge.reconciliation.premises import (
    normalize_signal,
    signal_premise,
)
from cortex.capabilities.graph_merge.reconciliation.receipts import (
    dependency_facts,
    policy_hash,
    premise_hash,
)
from cortex.capabilities.graph_merge.runtime.state import canonical_hash

_KIND = 'signal_scope_narrowing_v1'
_FIELDS = {'version', 'kind', 'source_receipt_hash', 'check_hash', 'source_policy_hash',
           'target_policy_hash', 'source_premise_hash', 'target_premise_hash',
           'audited_current_hash', 'old_audit_hash', 'transition_hash', 'subset_counts', 'record_hash'}


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _subset_rows(old, new, label):
    if label == 'evidence':
        # One public evidence handle can bind several source references. Keep
        # every exact source/body occurrence; ID-only indexing would either
        # reject valid attribution or silently erase distinct asset bindings.
        old_counts = Counter(canonical_hash(row) for row in old)
        new_counts = Counter(canonical_hash(row) for row in new)
        if new_counts - old_counts:
            raise ValueError('Scope migration is not an exact fact subset: evidence')
        return {'source': len(old), 'target': len(new)}

    def index(rows):
        result = {}
        for row in rows:
            ident = row.get('id')
            if not isinstance(ident, str) or ident in result:
                raise ValueError('Scope migration has invalid or duplicate ' + label)
            result[ident] = row
        return result
    old_by_id, new_by_id = index(old), index(new)
    if any(old_by_id.get(ident) != row for ident, row in new_by_id.items()):
        raise ValueError('Scope migration is not an exact fact subset: ' + label)
    return {'source': len(old_by_id), 'target': len(new_by_id)}


def _transition(engine, old_audit, source_policy_hash, transition):
    old_versions = old_audit.get('source_contract_versions')
    new_versions = engine.contract_versions.to_dict()
    if (not isinstance(old_versions, dict)
            or canonical_hash({'receipt_protocol': 1, 'contracts': old_versions}) != source_policy_hash):
        raise ValueError('Scope migration source contract versions do not match the reviewed policy')
    for key in ('schema', 'review_policy', 'task_state'):
        if old_versions.get(key) != new_versions.get(key) or key not in old_versions:
            raise ValueError('Scope migration changed a semantic contract: ' + key)
    if (not isinstance(transition, dict)
            or set(transition) != {'source_policy_hash', 'target_policy_hash', 'files'}
            or transition['source_policy_hash'] != source_policy_hash
            or transition['target_policy_hash'] != policy_hash(engine)
            or not isinstance(transition['files'], dict) or not transition['files']):
        raise ValueError('Scope migration requires an explicit reviewed source transition manifest')
    root = Path(__file__).resolve().parents[5]
    for relative, versions in transition['files'].items():
        path = Path(relative)
        if (path.is_absolute() or '..' in path.parts or not relative.startswith('src/cortex/capabilities/graph_merge/')):
            raise ValueError('Scope migration transition path is outside the reviewed runtime: ' + relative)
        if (not isinstance(versions, dict) or set(versions) != {'before', 'after'}
                or versions['before'] == versions['after'] or not _hash(versions['after'])
                or (versions['before'] is not None and not _hash(versions['before']))):
            raise ValueError('Scope migration has invalid transition file hashes')
        target = root / path
        if not target.is_file() or sha256(target.read_bytes()).hexdigest() != versions['after']:
            raise ValueError('Scope migration target file changed after transition review: ' + relative)
    return canonical_hash(transition)


def build_scope_migration(engine, receipt, old_audit, *, source_policy_hash, reviewed_transition):
    """Prove a narrower current scope was already covered by a valid old accept.

    ``old_audit`` is exported by frozen old code and contains current_hash,
    policy_hash, receipt_record_hash, and full premise={check,supplementary}.
    The caller explicitly supplies the reviewed source policy and a transition
    manifest with source/target policies and files={path:{before,after}} hashes.
    Target hashes are checked against this source tree. Schema, Review policy
    and task-state contracts must remain identical. Tools/prompts changes need
    the explicit external scope-only review; this is never an automatic path
    for arbitrary future policies.
    """
    if not _hash(source_policy_hash) or receipt['policy_hash'] != source_policy_hash:
        raise ValueError('Scope migration source policy is not the reviewed policy')
    transition_hash = _transition(engine, old_audit, source_policy_hash, reviewed_transition)
    ledger = getattr(engine, 'signal_receipts', [])
    if not any(row == receipt for row in ledger):
        raise ValueError('Scope migration requires the original accepted receipt')
    if latest_receipt(ledger, receipt['check'], policy_hash=source_policy_hash) != receipt:
        raise ValueError('Scope migration cannot revive a superseded source receipt')
    current_hash = canonical_hash(engine.current)
    if (old_audit.get('current_hash') != current_hash
            or old_audit.get('policy_hash') != source_policy_hash
            or old_audit.get('receipt_record_hash') != receipt['record_hash']):
        raise ValueError('Scope migration old audit is not bound to this snapshot and receipt')
    old = old_audit.get('premise')
    if not isinstance(old, dict) or set(old) != {'check', 'supplementary'}:
        raise ValueError('Scope migration requires complete recomputed old premise facts')
    if canonical_hash(old) != receipt['premise_hash']:
        raise ValueError('Scope migration original receipt no longer matches current old premises')
    new = signal_premise(engine.ctx, engine.current, receipt['check'])
    old_check = old['check']
    if old_check.get('version') == 2 and new.get('version') == 2:
        if old_check != new:
            raise ValueError('Same-version migration requires exactly unchanged signal premises')
    elif old_check.get('version') != 1 or new.get('version') != 2:
        raise ValueError('Scope migration supports reviewed v1-to-v2 narrowing or identical v2 premises')
    if (normalize_signal(old_check.get('signal')) != normalize_signal(receipt['check'])
            or normalize_signal(new['signal']) != normalize_signal(receipt['check'])
            or old_check.get('sql_dialect') != new.get('sql_dialect')):
        raise ValueError('Scope migration changed check requirements or SQL semantics')
    supplementary = dependency_facts(engine.ctx, engine.current, receipt['dependencies'])
    if old['supplementary'] != supplementary:
        raise ValueError('Scope migration changed original supplementary read facts')
    counts = {label: _subset_rows(old_check.get(label, []), new[label], label)
              for label in ('nodes', 'edges', 'contracts', 'evidence')}
    for label in ('missing_node_ids', 'missing_edge_ids', 'missing_contract_ids'):
        if not set(new[label]) <= set(old_check.get(label, [])):
            raise ValueError('Scope migration introduced a new missing-object premise: ' + label)
    target_hash = canonical_hash({'check': new, 'supplementary': supplementary})
    row = {'version': 1, 'kind': _KIND, 'source_receipt_hash': receipt['record_hash'],
           'check_hash': receipt['check_hash'], 'source_policy_hash': source_policy_hash,
           'target_policy_hash': policy_hash(engine), 'source_premise_hash': receipt['premise_hash'],
           'target_premise_hash': target_hash, 'audited_current_hash': current_hash,
           'old_audit_hash': canonical_hash(old_audit), 'transition_hash': transition_hash, 'subset_counts': counts}
    if row['target_policy_hash'] == source_policy_hash:
        raise ValueError('Scope migration requires an explicit policy transition')
    row['record_hash'] = canonical_hash(row)
    return row


def validate_scope_migrations(rows, *, ledger):
    """Validate proof integrity and original immutable accepted bindings."""
    if not isinstance(rows, list):
        raise TypeError('Scope migration proofs must be an array')
    by_hash = {row['record_hash']: row for row in ledger}
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != _FIELDS or type(row['version']) is not int or row['version'] != 1
                or row['kind'] != _KIND):
            raise ValueError('Invalid scope migration proof fields or version')
        for name in _FIELDS - {'version', 'kind', 'subset_counts'}:
            if not _hash(row[name]):
                raise ValueError('Invalid scope migration proof digest: ' + name)
        if row['record_hash'] != canonical_hash({k: v for k, v in row.items() if k != 'record_hash'}):
            raise ValueError('Scope migration proof integrity mismatch')
        identity = (row['source_receipt_hash'], row['target_policy_hash'])
        if identity in seen:
            raise ValueError('Duplicate scope migration proof')
        seen.add(identity)
        source = by_hash.get(row['source_receipt_hash'])
        if source is None or any(source[key] != row[other] for key, other in (
                ('check_hash', 'check_hash'), ('policy_hash', 'source_policy_hash'),
                ('premise_hash', 'source_premise_hash'))):
            raise ValueError('Scope migration lost its original accepted binding')
        if row['source_policy_hash'] == row['target_policy_hash']:
            raise ValueError('Scope migration has no policy transition')
        counts = row['subset_counts']
        if not isinstance(counts, dict) or set(counts) != {'nodes', 'edges', 'contracts', 'evidence'}:
            raise ValueError('Scope migration has incomplete subset counts')
        if any(not isinstance(values, dict) or set(values) != {'source', 'target'}
               or any(type(value) is not int or value < 0 for value in values.values())
               or values['target'] > values['source'] for values in counts.values()):
            raise ValueError('Scope migration has invalid subset counts')
    return deepcopy(rows)


def migrated_receipt(engine, check):
    """Return an original receipt only while its audited narrowed facts match."""
    ledger = getattr(engine, 'signal_receipts', [])
    target_policy = policy_hash(engine)
    # A real Assessment under the target policy takes precedence, even if its
    # premises later change. An older compatibility proof cannot override it.
    if latest_receipt(ledger, check, policy_hash=target_policy) is not None:
        return None
    proofs = validate_scope_migrations(getattr(engine, 'signal_scope_migrations', []), ledger=ledger)
    check_hash = canonical_hash(normalize_signal(check))
    by_hash = {row['record_hash']: row for row in ledger}
    for proof in reversed(proofs):
        if proof['target_policy_hash'] != target_policy or proof['check_hash'] != check_hash:
            continue
        receipt = by_hash[proof['source_receipt_hash']]
        if latest_receipt(ledger, check, policy_hash=receipt['policy_hash']) != receipt:
            continue
        if premise_hash(engine, check, receipt['dependencies']) == proof['target_premise_hash']:
            return deepcopy(receipt)
    return None
