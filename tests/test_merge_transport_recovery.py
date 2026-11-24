from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from graph_merge_chat import Chat

from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery


def test_unrecoverable_error_is_not_retried():
    class Failed(Chat):
        def invoke(self, messages, config=None):
            self.requests.append(list(messages))
            raise ValueError("invalid configuration")

    model = Failed([])
    agent = SimpleNamespace(llm=model, recover_transport=lambda error: None)
    with pytest.raises(ValueError, match="invalid configuration"):
        invoke_with_recovery(agent, [], [], config={})
    assert len(model.requests) == 1


def test_repeated_upstream_truncation_stops_without_restarting_whole_merge():
    from cortex.capabilities.graph_merge.llm.transport import TransportRecoveryStopped
    from cortex.infrastructure.llm.stream_audit import UpstreamResponseIncomplete
    calls=[]
    class Failed:
        def invoke(self,messages,config=None):
            calls.append(list(messages));raise UpstreamResponseIncomplete('Upstream stream truncated')
    recovered=[];model=Failed()
    def recover(error):recovered.append(error);return model
    agent=SimpleNamespace(llm=model,recover_transport=recover)
    messages=[ToolMessage('Previously read evidence',tool_call_id='read')]
    with pytest.raises(TransportRecoveryStopped) as failure:
        invoke_with_recovery(agent,[],messages,config={})
    assert len(calls)==3 and len(recovered)==2
    assert all(c==messages for c in calls)
    assert failure.value.retryable is False
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('terminal_transport_runner',Path(__file__).parents[1]/'scripts/run_resilient_training.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    assert not runner.transient(failure.value)


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504])
def test_wrapped_http_errors_share_the_same_bounded_recovery(status):
    import httpx
    from openai import APIStatusError
    from cortex.capabilities.graph_merge.llm.transport import TransportRecoveryStopped
    calls, recoveries = [], []
    class Failed:
        def invoke(self, messages, config=None):
            calls.append(list(messages))
            inner = APIStatusError('upstream failure', response=httpx.Response(status, request=httpx.Request('POST', 'https://offline.invalid')), body={})
            raise RuntimeError('SDK wrapper without status_code') from inner
    model = Failed()
    def recover(error):
        recoveries.append(error)
        return model
    agent = SimpleNamespace(llm=model, recover_transport=recover)
    messages = [ToolMessage('Existing evidence', tool_call_id='read')]
    with pytest.raises(TransportRecoveryStopped) as error:
        invoke_with_recovery(agent, [], messages, config={})
    assert len(calls) == 3 and len(recoveries) == 2
    assert all(row == messages for row in calls)
    assert error.value.retryable is False


def test_success_starts_a_fresh_failure_window_for_the_next_logical_call():
    import httpx
    from openai import InternalServerError
    from cortex.capabilities.graph_merge.llm.transport import TransportRecoveryStopped
    calls = []
    class Model:
        def invoke(self, messages, config=None):
            calls.append(list(messages))
            if len(calls) == 3:
                return AIMessage(content='Completed first logical call')
            raise InternalServerError('upstream', response=httpx.Response(502, request=httpx.Request('POST', 'https://offline.invalid')), body={})
    model = Model()
    agent = SimpleNamespace(llm=model, recover_transport=lambda error: model)
    assert invoke_with_recovery(agent, [], [], config={}).content == 'Completed first logical call'
    with pytest.raises(TransportRecoveryStopped):
        invoke_with_recovery(agent, [], [], config={})
    assert len(calls) == 6


def test_canonical_transport_recovery_preserves_reads_without_replaying_them(tmp_path):
    from graph_merge_chat import ScriptedModel
    from test_reconciliation_agent_runtime import fixture, call
    from test_merge_agent_recovery_resume import ACCEPT
    from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore

    first = ScriptedModel([])
    engine, task = fixture(tmp_path, first)
    evidence_id = engine.session.inputs.evidence[0].id
    first.replies = iter([call('read_evidence', {'ids': [evidence_id]}), ConnectionError('Logical request interrupted')])
    replacement = ScriptedModel([call('edit_draft', {}), ACCEPT])
    replacement.engine = engine
    failures = []
    def recover(error):
        failures.append(error)
        return replacement
    engine.agent.recover_transport = recover
    engine._run_task(task, 'transport')
    assert len(failures) == 1 and len(first.requests) == 2 and len(replacement.requests) == 2
    assert first.requests[-1] == replacement.requests[0]
    saved = WorkspaceStore(engine.workspace_path).load('transport')
    writer = next(row for row in saved['task_state']['agent_recovery'].values()
                  if row.get('scope', {}).get('phase') == 'reconcile')
    assert [row['name'] for row in writer['runtime']['journal']].count('read_evidence') == 1
    assert len(engine.session.history) == 1


def test_current_node_assessment_cannot_restart_a_terminal_transport_failure():
    import httpx
    from openai import InternalServerError
    from test_node_local_context import context
    from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
    from cortex.capabilities.graph_merge.llm.transport import TransportRecoveryStopped

    class Failed(Chat):
        def invoke(self, messages, config=None):
            self.requests.append(list(messages))
            raise InternalServerError('upstream unavailable', response=httpx.Response(
                502, request=httpx.Request('POST', 'https://offline.invalid')), body={})
    model = Failed([])
    agent = IncrementalIntegrationAgent(model, max_calls=None)
    recovered = []
    def recover(error):
        recovered.append(error)
        return model
    agent.recover_transport = recover
    with pytest.raises(TransportRecoveryStopped):
        agent.invoke('node_assess', context(), {'task_id': 'node-group'})
    assert len(model.requests) == 3 and len(recovered) == 2
    assert all(request == model.requests[0] for request in model.requests)
