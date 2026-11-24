"""Adapters must consume provider SSE, including tools and usage, across loops."""
import asyncio
import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI
from cortex.infrastructure.llm.pool import (
    KeyScheduler,
    PooledChatModel,
    ThreadedChatModel,
)


def test_disabled_deepseek_thinking_omits_unsupported_reasoning_effort_on_wire(monkeypatch):
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import providers

    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "reasoning_effort" in payload:
            return httpx.Response(400, json={"error": {"message": "reasoning_effort is not supported"}})
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(providers, "DeepSeekChatOpenAI",
                            lambda **kwargs: DeepSeekChatOpenAI(http_client=client, **kwargs))
        model = providers.create_chat_model(LLMConfig(
            model="deepseek-v4-pro", base_url="https://offline.invalid/v1", api_key="test-only",
            enable_thinking=False, max_retries=0,
        ))
        assert model.invoke([HumanMessage(content="Reply OK")]).content == "OK"
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in requests[0]


def test_disabled_thinking_preserves_explicit_none_on_actual_wire(monkeypatch):
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm import providers

    requests = []

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0,
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        monkeypatch.setattr(providers, "DeepSeekChatOpenAI",
                            lambda **kwargs: DeepSeekChatOpenAI(http_client=client, **kwargs))
        model = providers.create_chat_model(LLMConfig(
            model="deepseek-v4-pro", base_url="https://offline.invalid/v1", api_key="test-only",
            enable_thinking=False, reasoning_effort="none", max_retries=0,
        ))
        assert model.invoke([HumanMessage(content="Reply OK")]).content == "OK"
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert requests[0]["reasoning_effort"] == "none"


def test_opt_in_native_transport_keeps_configured_none_without_sending_unsupported_field():
    requests=[]
    def transport(request):
        payload=json.loads(request.content);requests.append(payload)
        return httpx.Response(200,json={'id':'native','object':'chat.completion','created':0,'model':'deepseek-v4-pro',
            'choices':[{'index':0,'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model=DeepSeekChatOpenAI(model='deepseek-v4-pro',api_key='test-only',base_url='https://offline.invalid',
            http_client=client,reasoning_effort='none',extra_body={'thinking':{'type':'disabled'}},
            wire_native_thinking_only=True,max_retries=0)
        assert model.invoke([HumanMessage(content='Reply OK')]).content=='OK'
        assert model.reasoning_effort=='none'
    assert 'reasoning_effort' not in requests[0] and requests[0]['thinking']=={'type':'disabled'}


@pytest.mark.parametrize('adapter', ['threaded', 'pooled'])
def test_streaming_adapter_consumes_sse_and_preserves_tool_calls_and_usage(adapter):
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        chunks = [
            {'id': 'test', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'test',
             'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': '',
                 'tool_calls': [{'index': 0, 'id': 'call-1', 'type': 'function',
                                 'function': {'name': 'lookup', 'arguments': '{"id":'}}]}, 'finish_reason': None}]},
            {'id': 'test', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'test',
             'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
                 'function': {'arguments': '7}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'id': 'test', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'test',
             'choices': [], 'usage': {'prompt_tokens': 9, 'completion_tokens': 4, 'total_tokens': 13}},
        ]
        body = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks) + 'data: [DONE]\n\n'
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        provider = DeepSeekChatOpenAI(model='test', api_key='test-private-key', base_url='https://offline.invalid',
                                     http_client=client, streaming=True, stream_usage=True, max_retries=0)
        model = ThreadedChatModel(provider) if adapter == 'threaded' else PooledChatModel((provider,), KeyScheduler(1))
        bound = model.bind_tools([{'type': 'function', 'function': {'name': 'lookup',
                                  'parameters': {'type': 'object', 'properties': {'id': {'type': 'integer'}}}}}])
        for _ in range(2):
            result = asyncio.run(bound.ainvoke([HumanMessage(content='Look up id 7')]))
            assert result.tool_calls == [{'name': 'lookup', 'args': {'id': 7}, 'id': 'call-1', 'type': 'tool_call'}]
            assert result.usage_metadata['total_tokens'] == 13
        assert all(request['stream'] is True for request in requests)
        assert len(requests) == 2
