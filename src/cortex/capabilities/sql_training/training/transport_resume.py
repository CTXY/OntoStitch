"""Explicit endpoint aliases for an otherwise identical persisted case context."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from langgraph.checkpoint.sqlite import SqliteSaver

from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
from cortex.capabilities.sql_training.graph.snapshots import fingerprint


def resume_transport_context(contract, *, case_root, plan_path):
    """Keep the native context ID only when changing base_url alone reproduces it.

    This never edits checkpoints or acceptance evidence, and never aliases a
    model, source, database, prompt, schema, gate, or semantic configuration.
    """
    current_id = fingerprint(contract)
    checkpoint = Path(case_root) / 'checkpoints.sqlite'
    if not checkpoint.is_file():
        return contract, current_id
    source_id = contract['source']['source_id']
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        saved = saver.get({'configurable': {'thread_id': 'case-' + fingerprint(source_id), 'checkpoint_ns': ''}})
    values = (saved or {}).get('channel_values', {})
    expected = values.get('context_id')
    if expected is None or expected == current_id:
        return contract, current_id
    plan_path = Path(plan_path)
    plan = json.loads(plan_path.read_text())
    for previous_url in plan['allowed_source_base_urls']:
        compatible = deepcopy(contract)
        compatible['config']['llm']['base_url'] = previous_url
        if fingerprint(compatible) != expected:
            continue
        write_json_atomic(Path(case_root) / 'transport_resume.json', {
            'time': time.time(), 'case_id': source_id,
            'compatible_context_id': expected, 'actual_context_id': current_id,
            'previous_base_url': previous_url, 'actual_base_url': contract['config']['llm']['base_url'],
            'only_compatible_difference': 'config.llm.base_url',
            'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            'native_checkpoint_unchanged': True,
        })
        return compatible, expected
    raise ValueError('Transport resume refused: changing only an explicitly allowed base_url does not reproduce the native context fingerprint')
