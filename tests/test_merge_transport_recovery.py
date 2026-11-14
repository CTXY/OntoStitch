from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_graph_integration import catalog, concept, state
from test_incremental_agent import Chat

from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.llm.reconciliation import invoke_relation
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery
from cortex.schema import load_root_schema


def test_recovery_preserves_tool_results_without_repeating_reads():
    ctx = IntegrationContext(state(), state([*catalog(), concept("N", "a")]), load_root_schema())
    ctx.candidate = ctx.union_state()
    ident = ctx.input_node_map["N"]
    read = AIMessage(content="", tool_calls=[{
        "name": "inspect_items", "args": {"ids": [ident], "fields": ["targets"]},
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

    agent = SimpleNamespace(llm=first, trace_sink=None, call_budget=None,
                            max_calls=None, recover_transport=recover)
    result = invoke_relation(agent, "graph_assess", ctx, {
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
