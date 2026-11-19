from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_graph_integration import catalog, concept, state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery
from cortex.schema import load_root_schema


def test_recovery_preserves_tool_results_without_repeating_reads():
    ctx = IntegrationContext(state(), state([*catalog(), concept("N", "a")]), load_root_schema())
    ctx.candidate = ctx.union_state()
    ident = ctx.input_node_map["N"]
    read = AIMessage(content="", tool_calls=[{
        "name": "inspect_graph", "args": {"mode": "items", "ids": [ident], "fields": ["properties.targets"]},
        "id": "read-once", "type": "tool_call",
    }])

    class Broken(Chat):
        def invoke(self, messages, config=None):
            if self.requests:
                self.requests.append(list(messages))
                raise RuntimeError("[Errno 32] Broken pipe")
            return super().invoke(messages, config=config)

    first = Broken([read])
    replacement = Chat([AIMessage(content='{"verdict":"accept"}')])
    failures = []

    def recover(error):
        failures.append(str(error))
        return replacement

    agent = IncrementalIntegrationAgent(first, max_calls=None)
    agent.recover_transport = recover
    result = agent.invoke("graph_assess", ctx, {
        "task_id": "unit", "nodes": [], "edges": [], "evidence": [],
    })
    assert result.verdict == "accept"
    assert failures == ["[Errno 32] Broken pipe"]
    assert first.requests[-1] == replacement.requests[0]
    replies = [m for m in replacement.requests[0] if isinstance(m, ToolMessage)]
    assert len(replies) == 1 and replies[0].tool_call_id == "read-once"
    assert len(first.requests) == 2 and len(replacement.requests) == 1


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


def test_node_agent_cannot_bypass_http502_stop_with_its_outer_retry():
    import httpx
    from openai import InternalServerError
    from cortex.capabilities.graph_merge.llm.incremental import IncrementalIntegrationAgent
    from cortex.capabilities.graph_merge.llm.transport import TransportRecoveryStopped

    class Failed(Chat):
        def invoke(self, messages, config=None):
            self.requests.append(list(messages))
            raise InternalServerError('upstream unavailable', response=httpx.Response(
                502, request=httpx.Request('POST', 'https://offline.invalid')), body={})

    model = Failed([])
    ctx = IntegrationContext(state(), state([*catalog(), concept('N', 'a')]), load_root_schema())
    ctx.candidate = ctx.union_state()
    agent = IncrementalIntegrationAgent(model, max_calls=None)
    recovered = []
    def recover(error):
        recovered.append(error)
        return model
    agent.recover_transport = recover
    with pytest.raises(TransportRecoveryStopped):
        agent.invoke('node_assess', ctx, {'task_id': 'review', 'candidate_nodes': [],
            'candidate_relations': [], 'evidence': [], 'matched_groups': []})
    assert len(model.requests) == 3
    assert len(recovered) == 2
    assert all(messages == model.requests[0] for messages in model.requests)
