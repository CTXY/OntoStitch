"""Bounded account-key rotation preserves disjoint worker credential pools."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('operator_credential_runner', SCRIPTS / 'continue_reviewed_merge.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class StatusFailure(Exception):
    def __init__(self, status, message, body=None, *, response_only=False):
        super().__init__(message)
        self.status_code = None if response_only else status
        self.response = SimpleNamespace(status_code=status)
        self.body = body


@pytest.mark.parametrize('error', [StatusFailure(401, 'Invalid API key'),
    StatusFailure(403, 'Insufficient balance'),
    StatusFailure(403, 'Account rejected', {'error': {'type': 'billing_error'}}, response_only=True),
    StatusFailure(403, '余额不足')])
def test_authentication_and_explicit_billing_failures_rotate(error):
    assert runner.credential_failure(error)


def test_cause_and_context_are_inspected_without_cycles():
    outer = RuntimeError('Provider wrapper')
    billing = StatusFailure(403, 'Provider reports insufficient_balance')
    outer.__cause__ = ValueError('SDK wrapper')
    outer.__context__ = billing
    billing.__context__ = outer
    assert runner.credential_failure(outer)


@pytest.mark.parametrize('message', ['Permission denied', 'Insufficient permissions',
                                    'Account cannot access this model'])
def test_generic_forbidden_is_not_retried_even_with_old_socket_context(message):
    error = StatusFailure(403, message)
    error.__context__ = ConnectionError('An earlier socket error')
    assert not runner.credential_failure(error)
    assert not runner.transport_failure(error)


def test_billing_detection_is_not_a_generic_text_match_on_unrelated_status():
    assert not runner.credential_failure(StatusFailure(400, 'Insufficient balance'))
    assert not runner.credential_failure(RuntimeError('billing_error'))


def test_all_rejected_keys_stop_without_wrapping_to_an_unavailable_key():
    pool, unavailable, selected = [1, 3, 5], set(), [1]
    while True:
        unavailable.add(selected[-1])
        next_slot = runner.next_credential_slot(pool, selected[-1], unavailable)
        if next_slot is None:
            break
        selected.append(next_slot)
    assert selected == [1, 3, 5]
    assert unavailable == set(pool)
    assert runner.next_credential_slot(pool, 5, unavailable) is None


def test_workers_never_select_keys_from_the_other_pool():
    pools = ([0, 2, 4, 6], [1, 3, 5, 7])
    chosen = []
    for pool in pools:
        current, unavailable, seen = pool[0], set(), []
        while current is not None:
            seen.append(current)
            unavailable.add(current)
            current = runner.next_credential_slot(pool, current, unavailable)
        assert set(seen) == set(pool)
        chosen.append(set(seen))
    assert not chosen[0] & chosen[1]
    assert runner.next_credential_slot([0, 2, 4], 0, {2}) == 4


def test_runtime_pool_override_and_legacy_default(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'read_key_slots', lambda _path: [None] * 8)
    plan = {'credential_pool': [1, 3, 5]}
    assert runner.credential_pool(tmp_path, plan) == [1, 3, 5]
    (tmp_path / 'runtime_credentials.json').write_text(json.dumps({'credential_pool': [1, 3, 5, 7]}))
    assert runner.credential_pool(tmp_path, plan) == [1, 3, 5, 7]
    assert plan['credential_pool'] == [1, 3, 5]


@pytest.mark.parametrize('bad', [[], [1, 1], [9], [True], ['1']])
def test_invalid_runtime_pool_rejected_before_model_creation(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(runner, 'read_key_slots', lambda _path: [None] * 8)
    (tmp_path / 'runtime_credentials.json').write_text(json.dumps({'credential_pool': bad}))
    with pytest.raises(ValueError, match='distinct valid configured key-slot'):
        runner.credential_pool(tmp_path, {'credential_pool': [1, 3, 5]})
