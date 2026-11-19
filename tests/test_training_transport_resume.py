from copy import deepcopy
import hashlib
import json

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.training.transport_resume import resume_transport_context


def saved_case(tmp_path):
    contract = {'source': {'source_id': 'case-1', 'sql': 'SELECT 1'}, 'config': {
        'llm': {'base_url': 'https://old.invalid/v1', 'model': 'same-model', 'temperature': 0},
        'max_rounds': 5}, 'database': {'snapshot': 'same'}, 'prompt': 'same', 'gate': 'three-correct'}
    path = tmp_path / 'checkpoints.sqlite'
    config = {'configurable': {'thread_id': 'case-' + fingerprint('case-1'), 'checkpoint_ns': ''}}
    with SqliteSaver.from_conn_string(str(path)) as saver:
        checkpoint = empty_checkpoint()
        checkpoint['channel_values'] = {'context_id': fingerprint(contract), 'case_id': 'case-1', 'round': 3}
        saver.put(config, checkpoint, {}, {})
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'allowed_source_base_urls': ['https://old.invalid/v1']}))
    return contract, path, plan


def test_endpoint_resume_preserves_native_checkpoint_and_semantic_context(tmp_path):
    previous, checkpoint, plan = saved_case(tmp_path)
    original_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    current = deepcopy(previous)
    current['config']['llm']['base_url'] = 'https://new.invalid/v1'
    compatible, ident = resume_transport_context(current, case_root=tmp_path, plan_path=plan)
    assert compatible == previous and ident == fingerprint(previous)
    assert current['config']['llm']['base_url'] == 'https://new.invalid/v1'
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == original_hash
    assert json.loads((tmp_path / 'transport_resume.json').read_text())['only_compatible_difference'] == 'config.llm.base_url'


@pytest.mark.parametrize('change', ['source', 'model', 'rounds', 'database', 'prompt', 'gate'])
def test_endpoint_alias_never_permits_semantic_changes(tmp_path, change):
    current, checkpoint, plan = saved_case(tmp_path)
    original_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    current['config']['llm']['base_url'] = 'https://new.invalid/v1'
    if change == 'source':
        current['source']['sql'] = 'SELECT 2'
    elif change == 'model':
        current['config']['llm']['model'] = 'different'
    elif change == 'rounds':
        current['config']['max_rounds'] = 6
    else:
        current[change] = 'different'
    with pytest.raises(ValueError, match='Transport resume refused'):
        resume_transport_context(current, case_root=tmp_path, plan_path=plan)
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == original_hash
    assert not (tmp_path / 'transport_resume.json').exists()
