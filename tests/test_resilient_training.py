"""Credential hygiene and unbounded merge episodes retain semantic validation."""
import json

import pytest
from langchain_core.messages import AIMessage
from test_agent_runtime import ScriptModel
from graph_merge_fixtures import catalog, concept, state

from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.schema import load_root_schema


def test_database_key_pool_stays_within_assigned_slots(tmp_path):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('restricted_key_pool', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    keys = tmp_path / 'keys'
    keys.write_text('\n'.join(f'test-key-{i}' for i in range(8)))
    args = SimpleNamespace(keys=keys, key_slots=[2, 5, 7])
    assert runner.selected_key_slots(args) == (2, 5, 7)
    assert [runner.next_key_slot(args, i) for i in [2, 5, 7]] == [5, 7, 2]
    assert runner.key_slot_arguments(args) == ['--key-slots', '2', '5', '7']
    for invalid in [[], [2, 2], [8], [-1]]:
        args.key_slots = invalid
        with pytest.raises(ValueError):
            runner.selected_key_slots(args)


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


def test_selected_credentials_receive_independent_request_limits(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    import httpx
    from cortex.config.models import CortexConfig, LLMConfig, SQLiteDatabaseConfig

    spec = importlib.util.spec_from_file_location('credential_request_scopes', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    keys = tmp_path / 'pool.keys'
    keys.write_text('credential-alpha\ncredential-beta\n')
    config = CortexConfig(llm=LLMConfig(model='test', base_url='https://offline.invalid'),
                          database=SQLiteDatabaseConfig(path=tmp_path / 'database.sqlite'))
    clients, selected = [], []

    def model(llm, **kwargs):
        selected.append(llm.api_key.get_secret_value())
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'ok')))
        clients.append(client)
        return SimpleNamespace(root_client=SimpleNamespace(_client=client))

    monkeypatch.setattr(runner, 'load_config', lambda _: config)
    monkeypatch.setattr(runner, 'create_chat_model', model)
    monkeypatch.setenv('CORTEX_LLM_REQUEST_LEASE_DIR', str(tmp_path / 'leases'))
    monkeypatch.setenv('CORTEX_LLM_REQUEST_LIMIT_SCOPE', 'credential')
    monkeypatch.setenv('CORTEX_LLM_MAX_INFLIGHT_REQUESTS', '1')
    try:
        for slot in [0, 1, 0]:
            runner.configuration(SimpleNamespace(config=tmp_path / 'config.yaml', keys=keys,
                model='test', slot=slot), tmp_path / 'workspace')
        assert selected == ['credential-alpha', 'credential-beta', 'credential-alpha']
        scopes = [client._cortex_request_limit for client in clients]
        assert scopes[0] == scopes[2] and scopes[0] != scopes[1]
        assert all(scope[1] == 1 for scope in scopes)
        assert all(client.get('https://offline.invalid/check').content == b'ok' for client in clients)
    finally:
        for client in clients:
            client.close()


def test_uncapped_merge_still_stops_repeated_protocol_failure_without_accepting():
    from cortex.capabilities.graph_merge.llm.recovery import OutputProtocolRecoveryExhausted

    context = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]), load_root_schema())
    original = context.union_state().model_dump(mode='json')
    model = ScriptModel([*[AIMessage(content=f'not JSON attempt {i}') for i in range(12)],
                         AIMessage(content=json.dumps({'groups': [], 'distinctions': []}))])
    agent = IncrementalIntegrationAgent(model, max_calls=None)
    # No configured call cap does not disable the current no-progress policy:
    # changed error narration supplies no new graph facts or valid protocol.
    with pytest.raises(OutputProtocolRecoveryExhausted):
        agent.invoke('identity', context, {'incoming_seeds': []})
    assert len(model._calls) == 3
    assert context.union_state().model_dump(mode='json') == original


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


def test_shared_credential_runs_isolated_cases_concurrently_before_merge(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location('shared_credential_runner', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    keys = tmp_path / 'pool.keys'
    keys.write_text('single-test-credential')
    runner.write(tmp_path / 'manifest.json', {'question_ids': ['one', 'two', 'three']})
    for ident in ['one', 'two', 'three']:
        (tmp_path / 'cases' / ident).mkdir(parents=True)
    commands, concurrency, merged = [], [0, 0], []

    class Process:
        def __init__(self, command, **kwargs):
            self.pid = 123 + len(commands)
            self.done = False
            commands.append(command)
            concurrency[0] += 1
            concurrency[1] = max(concurrency)
            ident = command[command.index('--case') + 1]
            runner.write(tmp_path / 'cases' / ident / 'status.json', {'status': 'passed'})

        def poll(self):
            if not self.done:
                concurrency[0] -= 1
                self.done = True
            return 0

    monkeypatch.setattr(runner.subprocess, 'Popen', Process)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    monkeypatch.setattr(runner, 'merge_worker', lambda args: merged.append(args.slot))
    runner.supervise(SimpleNamespace(output=tmp_path, keys=keys, config=tmp_path / 'config.yaml',
        model='test', key_slots=[0], case_workers=3, slot=0))
    assert concurrency[1] == 3
    assert {c[c.index('--case') + 1] for c in commands} == {'one', 'two', 'three'}
    assert all(c[c.index('--slot') + 1] == '0' for c in commands)
    assert merged == [0]
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
    assert len(list((tmp_path / 'wire').glob('*_request.json'))) == 3
    assert len(list((tmp_path / 'wire').glob('*_response.json'))) == 3
    assert len(list((tmp_path / 'wire').glob('*_headers.json'))) == 3
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
    assert runner.transient(RuntimeError('[Errno 32] Broken pipe'))
    assert runner.transient(RuntimeError('Connection reset by peer'))
    assert not runner.transient(RuntimeError('APIError: insufficient balance'))
    assert not runner.transient(RuntimeError("Error code: 500 - Failed to validate API key"))
    assert not runner.transient(RuntimeError('Error code: 401 - invalid_api_key'))


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


def test_merge_worker_submits_multiple_case_graphs_per_operator_call(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    import cortex.capabilities.graph_merge.engine as integration

    spec = importlib.util.spec_from_file_location('batch_runner', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.write(tmp_path / 'manifest.json', {'question_ids': ['one', 'two', 'three']})
    for ident in ['one', 'two', 'three']:
        runner.write(tmp_path / 'cases' / ident / 'input.json', {
            'source_id': ident, 'question': 'Question', 'sql': 'SELECT 1', 'evidence': None})
        runner.write(tmp_path / 'cases' / ident / 'state.json',
            state([*catalog(), concept('same-local-id', 'a')]).model_dump(mode='json'))
    calls = []

    class Engine:
        def __init__(self, *args, **kwargs):
            pass

        def merge(self, current, incoming, schema, **kwargs):
            calls.append(sum(n.node_type == 'concept' for n in incoming.graph.nodes))
            return SimpleNamespace(state=current, model_dump=lambda **kwargs: {})

    monkeypatch.setattr(integration, 'IncrementalGraphIntegrationEngine', Engine)
    monkeypatch.setitem(sys.modules, 'audit_incremental_merge', SimpleNamespace(audit_result=lambda *args: {'status': 'passed'}))
    from cortex.config.models import GraphMergeConfig
    configured = SimpleNamespace(services=SimpleNamespace(graph_merge=GraphMergeConfig()))
    monkeypatch.setattr(runner, 'configuration', lambda *args: (configured, object()))
    monkeypatch.setattr(runner, 'observe', lambda *args: None)
    monkeypatch.setattr(runner, 'render_graph_html', lambda *args, **kwargs: 'graph')
    runner.merge_worker(SimpleNamespace(output=tmp_path, merge_batch_size=2, slot=0))
    assert calls == [2, 1]
    assert runner.read(tmp_path / 'merge_status.json')['merged'] == ['one', 'two', 'three']
    maps = runner.read(tmp_path / 'merge/batch-0001/case_input_mappings.json')
    assert maps['one']['nodes']['same-local-id'] != maps['two']['nodes']['same-local-id']


def test_multi_graph_input_preserves_per_case_relations_and_business_meaning():
    from graph_merge_fixtures import edge

    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.sql_training.merge_evaluation import (
        _combined_incoming_state,
    )

    parent = concept('parent', 'a').model_copy(update={'properties': {
        'targets': [['a']], 'definition': 'The two evidenced classifications are compatible categories.'}})
    graph = state([*catalog(), parent, concept('child1', 'b'), concept('child2', 'c')],
                  [edge('x', 'child1', 'parent', 'is_a'), edge('y', 'child2', 'parent', 'is_a')]).graph
    bundle = _combined_incoming_state((('first', graph), ('second', graph)), state())
    # This is unmerged input: repeated physical identities and semantic
    # referents are intentional candidates, not a published final ontology.
    GraphManager(load_root_schema()).prepare_fragment(bundle, existing=state())
    context = IntegrationContext(state(), bundle, load_root_schema())
    assert len({v for v in context.physical_map.values()}) == len(catalog())
    assert len({n.id for n in bundle.graph.nodes}) == 2 * len(graph.nodes)
    for n in bundle.graph.nodes:
        if n.name == 'parent':
            assert n.properties['definition'] == parent.properties['definition']
            relations = [e for e in bundle.graph.edges if e.to_node_id == n.id and e.edge_type == 'is_a']
            assert len(relations) == 2
            assert {next(node.name for node in bundle.graph.nodes if node.id == edge.from_node_id) for edge in relations} == {'child1', 'child2'}


def test_empty_fatal_report_retains_original_error_without_indexing_cases(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace as N
    spec=importlib.util.spec_from_file_location('empty_report_runner',Path(__file__).parents[1]/'scripts/run_resilient_training.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    folder=tmp_path/'cases/one';folder.mkdir(parents=True)
    runner.write(folder/'input.json',{'source_id':'one','question':'Count rows','sql':'SELECT 1'})
    report=N(cases=(),errors=["ValueError: unresolved database identifier"])
    result=N(evaluation_report=report,status=N(value='failed'),model_dump=lambda **kw:{'evaluation_report':{'cases':[],'errors':report.errors}})
    monkeypatch.setattr(runner,'configuration',lambda *a:(N(),N()))
    monkeypatch.setattr(runner,'observe',lambda *a:None)
    monkeypatch.setattr(runner,'build_sql_extraction',lambda *a,**k:N())
    monkeypatch.setattr(runner,'extract_training',lambda *a:result)
    runner.case_worker(N(output=tmp_path,case='one',slot=0))
    status=runner.read(folder/'status.json')
    assert status['stage']=='extraction_initialization'
    assert status['errors']==report.errors and status['reason']==report.errors[0]


def test_preflight_failure_dispatches_no_case_workers(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace as N
    spec=importlib.util.spec_from_file_location('preflight_dispatch_runner',Path(__file__).parents[1]/'scripts/run_resilient_training.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    keys=tmp_path/'pool.keys';keys.write_text('test-key')
    monkeypatch.setattr(runner,'prepare_catalog',lambda args:(_ for _ in ()).throw(ValueError('invalid physical schema')))
    monkeypatch.setattr(runner.subprocess,'Popen',lambda *a,**k:pytest.fail('Do not dispatch after preflight failure'))
    with pytest.raises(ValueError,match='invalid physical schema'):
        runner.supervise(N(output=tmp_path,keys=keys,prepared_catalog=tmp_path/'physical'))
    assert runner.read(tmp_path/'progress.json')['stage']=='catalog_preflight'
