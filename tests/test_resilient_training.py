"""Credential hygiene and unbounded merge episodes retain semantic validation."""
import json

import pytest
from langchain_core.messages import AIMessage
from test_agent_runtime import ScriptModel
from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import load_root_schema


def test_key_slots_are_ordered_private_and_validate_duplicates(tmp_path):
    path = tmp_path / 'pool.keys'
    path.write_text('# local configuration\nfirst-test-credential\n\nsecond-test-credential\n')
    slots = read_key_slots(path)
    assert [s.index for s in slots] == [0, 1]
    assert slots[1].api_key.get_secret_value() == 'second-test-credential'
    assert 'test-credential' not in repr(slots)
    path.write_text('duplicate\nduplicate\n')
    with pytest.raises(ValueError, match='duplicate'):
        read_key_slots(path)


def test_uncapped_merge_can_correct_format_beyond_twelve_calls():
    context = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    # Malformed output is never accepted; the thirteenth valid result can finish.
    model = ScriptModel([*[AIMessage(content='not JSON') for _ in range(12)],
                         AIMessage(content=json.dumps({'groups': [], 'distinctions': []}))])
    agent = IncrementalIntegrationAgent(model, max_calls=None)
    result = agent.invoke('identity', context, {'incoming_seeds': []})
    assert result.groups == () and result.distinctions == ()
    assert len(model._calls) == 13


def test_supervisor_restart_after_extraction_enters_merge_without_new_workers(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    script = Path(__file__).parents[1] / 'scripts/run_resilient_training.py'
    spec = importlib.util.spec_from_file_location('resilient_training_runner', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    keys = tmp_path / 'pool.keys'
    keys.write_text('test-credential')
    runner.write(tmp_path / 'manifest.json', {'question_ids': ['one']})
    runner.write(tmp_path / 'cases/one/status.json', {'status': 'passed'})
    merged = []
    monkeypatch.setattr(runner, 'merge_worker', lambda args: merged.append(True))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('No worker should restart'))
    runner.supervise(SimpleNamespace(output=tmp_path, keys=keys))
    assert merged == [True]
    assert runner.read(tmp_path / 'progress.json')['phase'] == 'finished'


def test_sdk_retries_identical_request_and_audit_counts_each_attempt(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    import httpx
    from langchain_core.messages import HumanMessage
    from openai import OpenAI

    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import create_chat_model

    requests = []

    def transport(request):
        requests.append(request.content)
        if len(requests) < 3:
            return httpx.Response(502, json={'error': {'message': 'temporary outage', 'type': 'upstream_error'}})
        return httpx.Response(200, json={'id': 'offline', 'model': 'deepseek-v4-pro', 'created': 0,
            'object': 'chat.completion', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})

    client = OpenAI(api_key='test-private-credential', base_url='https://offline.invalid/v1',
                    max_retries=8, timeout=3600, http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(client, '_sleep_for_retry', lambda **kwargs: None)
    model = create_chat_model(LLMConfig(model='deepseek-v4-pro', api_key='test-private-credential',
                                      base_url='https://offline.invalid/v1', enable_thinking=False, max_retries=8, timeout=3600))
    model.root_client, model.client = client, client.chat.completions
    script = Path(__file__).parents[1] / 'scripts/run_resilient_training.py'
    spec = importlib.util.spec_from_file_location('wire_test_runner', script)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.observe(model, tmp_path)
    assert model.invoke([HumanMessage(content='offline transport test')]).content == 'ok'
    assert len(requests) == 3 and len(set(requests)) == 1
    logs = list((tmp_path / 'wire').glob('*.json'))
    assert len(logs) == 6
    assert all('test-private-credential' not in p.read_text() for p in logs)
    client.close()


def test_transient_connection_exception_does_not_require_class_name_in_message():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location('recovery_classifier', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.transient(ConnectionError('peer closed'))
    assert not runner.transient(ValueError('invalid field'))
