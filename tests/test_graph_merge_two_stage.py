import json
import pytest
from graph_merge_fixtures import catalog, concept, state
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.schema import load_root_schema
"""Small submissions preserve previous work; graph repair rechecks changed meaning."""


def test_call_trace_survives_agent_error(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
    from cortex.infrastructure.artifacts.graph_merge_trace import (
        FileSystemGraphMergeTraceSink,
    )

    path = tmp_path / "calls.jsonl"
    trace = EpisodeTrace(FileSystemGraphMergeTraceSink(path), "reconcile")
    trace.on_chat_model_start({}, [[HumanMessage(content="input")]], run_id="one")
    trace.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="reply",
                            usage_metadata={
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id="one",
    )
    trace.on_chat_model_start({}, [[HumanMessage(content="repair")]], run_id="two")
    trace.on_llm_error(RuntimeError("upstream failure"), run_id="two")
    events = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["event_type"] for x in events] == [
        "model_start",
        "model_end",
        "model_start",
        "model_error",
    ]
    assert events[1]["message"]["usage_metadata"]["total_tokens"] == 5
    assert trace.calls == 2


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "base_url", ["https://api.deepseek.com/v1", "https://compatible.example/v1"]
)
def test_thinking_switch_uses_provider_supported_parameter(
    monkeypatch, enabled, base_url
):
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import providers

    monkeypatch.setattr(providers, "ChatOpenAI", lambda **kw: kw)
    monkeypatch.setattr(providers, "DeepSeekChatOpenAI", lambda **kw: kw)
    model = providers.create_chat_model(
        LLMConfig(
            model="configured-model",
            base_url=base_url,
            api_key="test-only",
            enable_thinking=enabled,
        )
    )
    assert model["extra_body"] == (
        {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if "api.deepseek.com" in base_url
        else {"enable_thinking": enabled}
    )


def test_selection_signal_uses_current_concept_targets_and_sql_dialect():
    from cortex.capabilities.graph_merge.graph.publication import Preview
    from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals, projected_columns
    from cortex.capabilities.graph_merge.evidence import EvidenceItem
    from cortex.schema import SourceReference

    evidence = (
        EvidenceItem(id='first', case_id='first', source=SourceReference(source_id='A'),
                     content={'sql': 'SELECT `a` FROM `objects` ORDER BY `b`'}),
        EvidenceItem(id='second', case_id='second', source=SourceReference(source_id='B'),
                     content={'sql': 'SELECT a, b FROM objects'}),
    )
    ctx = IntegrationContext(state(), state([*catalog(), concept('A', 'a')]),
                             load_root_schema(), evidence=evidence)
    ctx.sql_dialect = 'sqlite'
    current = ctx.union_state()
    owner, a, b = (ctx.input_node_map[key] for key in ('A', 'a', 'b'))
    current = current.model_copy(update={'graph': current.graph.model_copy(update={
        'nodes': tuple(node.model_copy(update={
            'properties': {**node.properties, 'targets': [[a], [b]]},
            'sources': tuple(item.source for item in evidence),
        }) if node.id == owner else node for node in current.graph.nodes)})})
    nodes = {node.id: node for node in current.graph.nodes}
    assert projected_columns(evidence[0].content['sql'], nodes, dialect=ctx.sql_dialect) == {a}
    assert projected_columns(evidence[1].content['sql'], nodes, dialect=ctx.sql_dialect) == {a, b}
    preview = Preview(current, {}, {}, [])
    signals = inspection_signals(ctx, preview)
    choices = next(signal for signal in signals if signal['kind'] == 'source_selection_variants')
    variants = {row['evidence_id']: row for row in choices['variants']}
    assert set(variants) == {'first', 'second'}
    assert variants['first']['selected_physical_inputs'] == [a]
    assert variants['second']['selected_physical_inputs'] == sorted([a, b])
    assert all(row['node_id'] == owner for row in variants.values())

    # Historical source contents and origin traversal are not premises of a
    # current-graph signal. The caller may still supply an origin index.
    class ForbiddenOrigins:
        def origin_refs(self, *_args):
            raise AssertionError('Signal detection must not traverse archived source nodes')
    ctx.raw_nodes[owner] = ctx.raw_nodes[owner].model_copy(update={
        'properties': {'targets': [[a]]}, 'sources': ()})
    assert inspection_signals(ctx, preview, origins=ForbiddenOrigins()) == signals
    narrowed = current.model_copy(update={'graph': current.graph.model_copy(update={
        'nodes': tuple(node.model_copy(update={'properties': {'targets': [[a]]}})
                       if node.id == owner else node for node in current.graph.nodes)})})
    assert not any(signal['kind'] == 'source_selection_variants'
                   for signal in inspection_signals(ctx, Preview(narrowed, {}, {}, [])))


def test_reasoning_survives_response_and_following_tool_turn():
    from langchain_core.messages import HumanMessage, ToolMessage

    from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI

    model = DeepSeekChatOpenAI(
        model="configured-model",
        api_key="test-only",
        base_url="https://api.deepseek.com",
    )
    response = {
        "model": "configured-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "External model reasoning",
                    "tool_calls": [
                        {
                            "id": "call1",
                            "type": "function",
                            "function": {"name": "inspect_graph", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    ai = model._create_chat_result(response).generations[0].message
    payload = model._get_request_payload(
        [
            HumanMessage(content="review"),
            ai,
            ToolMessage(content="facts", tool_call_id="call1"),
        ]
    )
    assert payload["messages"][1]["reasoning_content"] == "External model reasoning"
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.parametrize("enabled", [False, True])
def test_deepseek_thinking_protocol_survives_proxy_host(monkeypatch, enabled):
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import providers

    monkeypatch.setattr(providers, "ChatOpenAI", lambda **kw: kw)
    monkeypatch.setattr(providers, "DeepSeekChatOpenAI", lambda **kw: kw)
    model = providers.create_chat_model(LLMConfig(
        model="deepseek-v4-pro", base_url="https://proxy.example/v1", api_key="test-only",
        enable_thinking=enabled, reasoning_effort="high",
    ))
    assert model["extra_body"] == {"thinking": {"type": "enabled" if enabled else "disabled"}}
    assert model["reasoning_effort"] == "high"
