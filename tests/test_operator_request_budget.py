"""A complete Identity request may be larger; local Agent limits stay fixed."""
import importlib.util
import json
from pathlib import Path
import sys

from langchain_core.messages import HumanMessage
import pytest

from cortex.capabilities.graph_merge.llm.budget import RequestInputTooLarge, check_request

SCRIPTS = Path(__file__).parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('operator_request_budget_runner', SCRIPTS / 'continue_reviewed_merge.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_large_identity_packet_passes_without_widening_local_phase_limits(monkeypatch):
    messages = [HumanMessage('x' * 233000)]

    def measure(agent, _phase, _context, _payload):
        return check_request(messages, [], max_characters=agent.request_max_characters)

    monkeypatch.setattr(runner.IncrementalIntegrationAgent, 'invoke', measure)
    agent = runner.OperatorAgent(None, request_max_characters=210000, identity_request_max_characters=600000)
    assert agent.invoke('identity', None, {}) > 233000
    assert agent.request_max_characters == 210000
    for phase in ('consolidate', 'node_assess', 'reconcile', 'graph_assess', 'relation_merger'):
        with pytest.raises(RequestInputTooLarge):
            agent.invoke(phase, None, {})
        assert agent.request_max_characters == 210000


def test_identity_exception_restores_the_local_budget(monkeypatch):
    def fail(agent, _phase, _context, _payload):
        assert agent.request_max_characters == 600000
        raise RuntimeError('Controlled transport failure')

    monkeypatch.setattr(runner.IncrementalIntegrationAgent, 'invoke', fail)
    agent = runner.OperatorAgent(None, request_max_characters=210000, identity_request_max_characters=600000)
    with pytest.raises(RuntimeError, match='Controlled transport'):
        agent.invoke('identity', None, {})
    assert agent.request_max_characters == 210000


def test_runtime_override_preserves_other_limits_and_legacy_default(tmp_path):
    assert runner.runtime_limits(tmp_path) == {'identity_request_max_characters': 210000,
        'local_request_max_characters': 210000, 'node_context_max_characters': 110000}
    (tmp_path / 'runtime_limits.json').write_text(json.dumps({'identity_request_max_characters': 600000}))
    assert runner.runtime_limits(tmp_path) == {'identity_request_max_characters': 600000,
        'local_request_max_characters': 210000, 'node_context_max_characters': 110000}


@pytest.mark.parametrize('bad', [True, 200000, '600000'])
def test_invalid_runtime_override_is_rejected_before_model_construction(tmp_path, bad):
    (tmp_path / 'runtime_limits.json').write_text(json.dumps({'identity_request_max_characters': bad}))
    with pytest.raises(ValueError, match='integer at least 210000'):
        runner.runtime_limits(tmp_path)


@pytest.mark.parametrize('limit',[230000,500000])
def test_scoped_local_override_passes_the_two_real_boundary_failures(tmp_path, monkeypatch,limit):
    (tmp_path/'runtime_limits.json').write_text(json.dumps({
        'identity_request_max_characters':600000,'local_request_max_characters':limit}))
    limits=runner.runtime_limits(tmp_path)
    assert limits['node_context_max_characters']==110000
    sizes=[212383,212061]
    def measure(agent,phase,context,payload):
        return check_request([HumanMessage('x'*payload['characters'])],[],max_characters=agent.request_max_characters)
    monkeypatch.setattr(runner.IncrementalIntegrationAgent,'invoke',measure)
    agent=runner.OperatorAgent(None,request_max_characters=limits['local_request_max_characters'],
        identity_request_max_characters=limits['identity_request_max_characters'])
    for size in sizes:
        assert agent.invoke('reconcile',None,{'characters':size})>size
    assert agent.request_max_characters==limit
    with pytest.raises(RequestInputTooLarge):
        agent.invoke('reconcile',None,{'characters':limit+1})


@pytest.mark.parametrize('bad',[True,200000,'230000'])
def test_invalid_local_override_fails_before_model_construction(tmp_path,bad):
    (tmp_path/'runtime_limits.json').write_text(json.dumps({'local_request_max_characters':bad}))
    with pytest.raises(ValueError,match='Local request limit'):
        runner.runtime_limits(tmp_path)


def test_local_limit_cannot_exceed_identity_budget(tmp_path):
    (tmp_path/'runtime_limits.json').write_text(json.dumps({
        'local_request_max_characters':230000,'identity_request_max_characters':210000}))
    with pytest.raises(ValueError,match='at least the local'):
        runner.runtime_limits(tmp_path)
