"""Probe preflight, transport recovery and real-data fixture integrity."""
from pathlib import Path
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import run_relation_reconciliation_v2_smoke as smoke


def natural_probe_args(tmp_path, name):
    """A tiny saved graph exercises IO invariants without optional local datasets."""
    from test_relation_merger import fixture
    graph, _ = fixture()
    db, case = smoke.MERGER_CASES[name]
    args = SimpleNamespace(graph_root=tmp_path/'graphs', source_input_root=tmp_path/'inputs',
        output=tmp_path/'results', live=False, max_calls=8, input_limit=100000,
        used_slots=[], secrets=())
    graph_file = args.graph_root/db/'case_level_graphs'/case/'state.json'
    source_file = args.source_input_root/db/'cases'/case/'input.json'
    graph_file.parent.mkdir(parents=True)
    source_file.parent.mkdir(parents=True)
    graph_file.write_text(graph.model_dump_json())
    source_file.write_text(json.dumps({'source_id': 'first', 'question': 'What is A?', 'sql': 'SELECT 1'}))
    return args, graph, graph_file


@pytest.mark.parametrize('name', smoke.MERGER_CASES)
def test_natural_probe_retains_exact_saved_graph_without_padding(tmp_path, name):
    from cortex.capabilities.graph_merge.reconciliation.relation_merger import relation_groups
    args, graph, graph_file = natural_probe_args(tmp_path, name)
    before = graph_file.read_bytes()
    fixture = smoke.natural_relation_fixture(args, name)
    assert fixture.before == graph
    assert fixture.provenance['controlled_metric_replicas'] == 0
    assert fixture.provenance['controlled_changes'] == []
    assert fixture.provenance['case_ids'] == ['/'.join(smoke.MERGER_CASES[name])]
    assert len(relation_groups(fixture.before)) == 1
    result = smoke.run_merger(args, {'used': 0, 'limit': 8}, fixture)
    assert result['status'] == 'passed'
    assert result['groups'] == 1 and result['batch_size_limit'] == 10
    assert result['actual_batch_sizes'] == [1]
    assert result['offline_model_calls'] == 1 and result['completed_resume_model_calls'] == 0
    assert graph_file.read_bytes() == before


def test_offline_assessment_is_direct_json_not_a_review_tool(monkeypatch):
    from langchain_core.tools import StructuredTool
    seen = []
    monkeypatch.setattr(smoke, 'semantic_oracle', lambda candidate, fixture: seen.append(candidate))
    fixture = SimpleNamespace(signal={'id': 'signal'})
    context = SimpleNamespace(reconciliation_runtime=SimpleNamespace(), candidate='actual-candidate')
    chat = smoke.ScriptedChat(fixture, context)
    chat.bind_tools([StructuredTool.from_function(lambda ids: ids, name='read_nodes', description='read')])
    result = chat.invoke([])
    assert result.tool_calls == []
    assert json.loads(result.content)['feedback'] == []
    assert seen == ['actual-candidate'] and chat.counts == {'assessment': 1}


@pytest.mark.parametrize('names,valid', [(['read_nodes', 'read_relations', 'read_evidence'], True),
                                        (['read_nodes', 'Review'], False)])
def test_wire_audit_accepts_read_only_json_assessment_and_rejects_review_tool(tmp_path, names, valid):
    import reconciliation_probe_fixtures as probes
    folder = tmp_path/'wire'
    folder.mkdir()
    (folder/'001_request.json').write_text(json.dumps({'body': {
        'messages': [{'content': 'v2-smoke:abstract_predicate-review'}],
        'tools': [{'type': 'function', 'function': {'name': name}} for name in names]},
        'timeout': {'read': 120, 'connect': 30, 'write': 60, 'pool': 30}}))
    row, = probes.wire_evidence(SimpleNamespace(output=tmp_path, request_timeout=120),
                               SimpleNamespace(name='abstract_predicate'))
    assert row['permissions_valid'] is valid
    if valid:
        assert row['role'] == 'assessment' and row['assessment_output_protocol'] == 'structured_json'


@pytest.mark.parametrize('flags', [['--max-calls', '17'], ['--total-call-budget', '65'],
    ['--timeout', '601'], ['--input-limit', '200001']])
def test_invalid_probe_budget_stops_before_model_or_output(tmp_path, monkeypatch, flags):
    monkeypatch.setattr(sys, 'argv', ['probe', '--live', '--output', str(tmp_path/'absent'), *flags])
    with pytest.raises(SystemExit) as error:
        smoke.main()
    assert error.value.code == 2 and not (tmp_path/'absent').exists()


def test_transport_retry_changes_slot_and_redacts_credentials(tmp_path, monkeypatch):
    selected = []
    monkeypatch.setattr(smoke, 'model_for', lambda args, folder, slot: selected.append(slot) or object())
    args = SimpleNamespace(used_slots=[2], slots=[2, 1, 0], secrets=('private-test-key',))
    agent = SimpleNamespace()
    smoke.bind_recovery(args, agent, tmp_path)
    assert agent.recover_transport(TimeoutError('private-test-key timeout')) is not None
    assert agent.recover_transport(TimeoutError('private-test-key timeout')) is not None
    assert agent.recover_transport(TimeoutError('private-test-key timeout')) is None
    assert selected == [1, 0]
    assert 'private-test-key' not in (tmp_path/'transport_recovery.jsonl').read_text()
    assert agent.recover_transport(ValueError('semantic error')) is None


def test_model_factory_uses_resolved_profile_and_actual_selected_key(tmp_path, monkeypatch):
    import httpx
    import cortex.config
    import cortex.infrastructure.llm.providers
    import cortex.infrastructure.llm.key_slots
    import run_resilient_training
    from cortex.config.models import LLMConfig
    monkeypatch.setattr(cortex.config, 'load_config', lambda path: SimpleNamespace(llm=LLMConfig(
        model='model-from-environment', base_url='https://example.invalid/v1', api_key=SecretStr('base'))))
    monkeypatch.setattr(cortex.infrastructure.llm.key_slots, 'read_key_slots', lambda path: (
        SimpleNamespace(api_key=SecretStr('first')), SimpleNamespace(api_key=SecretStr('second'))))
    def create(config):
        resolved = config.resolve()
        return SimpleNamespace(root_client=SimpleNamespace(_client=SimpleNamespace(timeout=httpx.Timeout(1))),
            model_name=resolved.model, openai_api_key=resolved.api_key, metadata={})
    monkeypatch.setattr(cortex.infrastructure.llm.providers, 'create_chat_model', create)
    monkeypatch.setattr(run_resilient_training, 'observe', lambda *args: None)
    args = SimpleNamespace(config=tmp_path/'config', keys=tmp_path/'keys', model=None, timeout=420)
    model = smoke.model_for(args, tmp_path, 1)
    assert model.model_name == 'model-from-environment'
    assert model.openai_api_key.get_secret_value() == 'second'
    assert model.root_client._client.timeout.read == 420
    assert model.metadata['credential_slot'] == 1


def test_missing_resume_checkpoint_is_rejected_before_model_or_file_creation(tmp_path,monkeypatch):
    target=tmp_path/'missing-source'
    monkeypatch.setattr(sys,'argv',['probe','--live','--output',str(tmp_path/'new-output'),
        '--cases','value_concepts','--resume-case',str(target)])
    with pytest.raises(SystemExit) as failure: smoke.main()
    assert failure.value.code==2 and not target.exists() and not (tmp_path/'new-output').exists()


def test_resume_and_seed_flags_cannot_replay_initial_submission(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();(source/'workspace.sqlite').write_bytes(b'not opened by this test')
    monkeypatch.setattr(sys,'argv',['probe','--live','--output',str(tmp_path/'new-output'),
        '--cases','value_concepts','--resume-case',str(source),'--seed-empty-first'])
    with pytest.raises(SystemExit) as failure: smoke.main()
    assert failure.value.code==2 and (source/'workspace.sqlite').read_bytes()==b'not opened by this test'
