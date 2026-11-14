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


def test_completed_adopted_worker_releases_slot_even_if_pid_still_exists(tmp_path):
    import importlib.util
    import os
    from pathlib import Path

    spec = importlib.util.spec_from_file_location('completed_worker', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.write(tmp_path / 'status.json', {'status': 'passed'})
    assert runner.AdoptedWorker(os.getpid(), tmp_path).poll() == 0


def test_supervisor_adopts_live_worker_without_popen_returncode_attribute(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location('adoption_protocol', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    keys = tmp_path / 'pool.keys'
    keys.write_text('test-credential')
    runner.write(tmp_path / 'manifest.json', {'question_ids': ['one']})
    runner.write(tmp_path / 'cases/one/status.json', {'status': 'running'})
    runner.write(tmp_path / 'active_workers.json', {'0': {'case': 'one', 'pid': 123}})

    class Worker:
        def __init__(self, pid, folder):
            self.pid, self.folder, self.reads = pid, folder, 0

        def poll(self):
            self.reads += 1
            if self.reads == 1:
                return None
            runner.write(self.folder / 'status.json', {'status': 'passed'})
            return 0

    merged = []
    monkeypatch.setattr(runner, 'AdoptedWorker', Worker)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    monkeypatch.setattr(runner, 'merge_worker', lambda args: merged.append(True))
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('Do not duplicate live workers'))
    runner.supervise(SimpleNamespace(output=tmp_path, keys=keys))
    assert merged == [True]
    assert runner.read(tmp_path / 'active_workers.json') == {}


def test_wire_deadline_never_restarts_an_already_answered_request(tmp_path):
    import importlib.util
    import os
    import time
    from pathlib import Path

    spec = importlib.util.spec_from_file_location('wire_deadline', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    request = tmp_path / 'wire/000001_request.json'
    runner.write(request, {})
    os.utime(request, (time.time()-4000, time.time()-4000))
    assert runner.stalled_request(tmp_path) == request.name
    (tmp_path / 'transport_recoveries.jsonl').write_text('{"request":"000001_request.json"}\n')
    assert runner.stalled_request(tmp_path) is None
    (tmp_path / 'transport_recoveries.jsonl').unlink()
    runner.write(request.with_name('000001_response.json'), {'status': 200})
    assert runner.stalled_request(tmp_path) is None


def test_merge_deadline_ignores_historical_requests_before_supervisor_start(tmp_path):
    import importlib.util
    import os
    import time
    from pathlib import Path

    spec = importlib.util.spec_from_file_location('merge_deadline', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.write(tmp_path / 'merge_status.json', {'question_id': 'sample'})
    request = tmp_path / 'merge/sample/wire/000001_request.json'
    runner.write(request, {})
    runner.write(tmp_path / 'supervisor.pid', 1)
    now = time.time()
    os.utime(request, (now - 4000, now - 4000))
    assert runner.stalled_merge(tmp_path) is None
    os.utime(tmp_path / 'supervisor.pid', (now - 5000, now - 5000))
    assert runner.stalled_merge(tmp_path) == (request.parent.parent, request.name)
