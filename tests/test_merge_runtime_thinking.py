"""Per-run mode survives model/key replacement and checks actual outgoing fields."""
import json
from types import SimpleNamespace

import httpx
import pytest
from merge_model_runtime import configure_merge_model


def model(slot):
    return SimpleNamespace(reasoning_effort=None, extra_body={'other': 'retained'}, metadata={'credential_slot': slot},
        root_client=SimpleNamespace(_client=SimpleNamespace(event_hooks={'request': []})))


def test_no_profile_keeps_other_runs_unchanged(tmp_path):
    folder = tmp_path / 'merge/batch-0003'
    instance = model(2)
    assert configure_merge_model(instance, folder) is instance
    assert instance.reasoning_effort is None and instance.extra_body == {'other': 'retained'}


def test_disable_is_applied_to_each_replacement_model_and_actual_wire(tmp_path):
    folder = tmp_path / 'merge/batch-0003'
    folder.mkdir(parents=True)
    (tmp_path / 'runtime_model.json').write_text(json.dumps({'thinking_mode': 'disabled', 'reasoning_effort': 'none'}))
    for slot in (2, 4):
        instance = configure_merge_model(model(slot), folder)
        assert instance.reasoning_effort == 'none'
        assert instance.extra_body == {'other': 'retained', 'thinking': {'type': 'disabled'}}
        assert instance.metadata['credential_slot'] == slot
        guard = instance.root_client._client.event_hooks['request'][-1]
        guard(httpx.Request('POST', 'https://offline.invalid', json={'thinking': {'type': 'disabled'}, 'reasoning_effort': 'none'}))
        with pytest.raises(ValueError, match='actual wire'):
            guard(httpx.Request('POST', 'https://offline.invalid', json={'thinking': {'type': 'disabled'}}))
    assert json.loads((folder / 'model_configuration.json').read_text())['requested_thinking'] == 'disabled'


def test_inconsistent_profile_is_rejected_before_sending_requests(tmp_path):
    folder = tmp_path / 'merge/batch-0003'
    folder.mkdir(parents=True)
    (tmp_path / 'runtime_model.json').write_text(json.dumps({'thinking_mode': 'disabled', 'reasoning_effort': 'high'}))
    with pytest.raises(ValueError, match='runtime_model'):
        configure_merge_model(model(2), folder)
