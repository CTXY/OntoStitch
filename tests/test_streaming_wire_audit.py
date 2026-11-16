import importlib.util
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from cortex.infrastructure.llm.deepseek import DeepSeekChatOpenAI
from cortex.infrastructure.llm.stream_audit import (
    completion_from_sse,
    normalize_sse_frames,
)


def stream_text():
    chunks = [
        {"id": "reply", "model": "deepseek-v4-pro", "choices": [{"index": 0, "delta": {"role": "", "content": "", "reasoning_content": "Consider ", "tool_calls": [{"index": 0, "id": "read", "type": "function", "function": {"name": "inspect_graph", "arguments": '{"ids":'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "request.", "tool_calls": [{"index": 0, "function": {"arguments": '["N"]}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    return ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'


def test_sse_reconstruction_keeps_complete_tool_arguments_and_usage():
    body = completion_from_sse(stream_text())
    assert body['usage']['total_tokens'] == 15
    assert json.loads(body['choices'][0]['message']['tool_calls'][0]['function']['arguments']) == {'ids': ['N']}
    assert body['choices'][0]['finish_reason'] == 'tool_calls'
    assert body['choices'][0]['message']['reasoning_content'] == 'Consider request.'


@pytest.mark.parametrize('backend_name', ['httpx', 'httpx2'])
def test_streamed_gateway_empty_role_and_wire_audit_use_real_sdk_offline(tmp_path, backend_name):
    backend = pytest.importorskip(backend_name)
    spec = importlib.util.spec_from_file_location('stream_runner', Path(__file__).parents[1] / 'scripts/run_resilient_training.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        heartbeat = 'data: ' + json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}]}) + '\n'
        raw = (heartbeat + stream_text()).encode()

        class LiveBody(backend.SyncByteStream):
            def __iter__(self):
                yield raw[:100]
                assert (tmp_path / 'wire/000001_stream.sse').read_bytes() == raw[:100]
                yield raw[100:]

        return backend.Response(200, headers={'content-type': 'text/event-stream'}, stream=LiveBody())

    with backend.Client(transport=backend.MockTransport(transport)) as client:
        model = DeepSeekChatOpenAI(model='deepseek-v4-pro', api_key='offline-secret',
            http_client=client, streaming=True, stream_usage=True)
        runner.observe(model, tmp_path)
        message = model.invoke([HumanMessage('Read N')])
    assert isinstance(message, AIMessage)
    assert message.tool_calls[0]['args'] == {'ids': ['N']}
    assert message.usage_metadata['total_tokens'] == 15
    assert message.additional_kwargs['reasoning_content'] == 'Consider request.'
    assert requests[0]['stream'] is True
    response = runner.read(tmp_path / 'wire/000001_response.json')
    assert response['body']['usage']['total_tokens'] == 15
    assert (tmp_path / 'wire/000001_stream.sse').exists()
    progress = runner.read(tmp_path / 'wire/000001_progress.json')
    assert progress['completed'] and progress['chunks'] == 2
    assert progress['bytes'] > 100 and progress['last_byte_time'] >= progress['first_byte_time']
    assert runner.read(tmp_path / 'wire/000001_headers.json')['status'] == 200
    assert 'offline-secret' not in (tmp_path / 'wire/000001_request.json').read_text()


def test_valid_multiline_sse_is_not_split_into_independent_json_objects():
    text = 'data: {"choices":\ndata: []}\n\n'
    assert normalize_sse_frames(text).strip() == text.strip()


def test_stream_disconnect_preserves_received_bytes_and_underlying_cause(tmp_path):
    import httpx
    from langchain_core.messages import HumanMessage
    spec=importlib.util.spec_from_file_location('broken_stream_runner',Path(__file__).parents[1]/'scripts/run_resilient_training.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices": []}\n\n'
            raise httpx.RemoteProtocolError('stream disconnected')
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=BrokenBody()))) as client:
        model=DeepSeekChatOpenAI(model='deepseek-v4-pro',api_key='offline-secret',http_client=client,streaming=True,max_retries=0)
        runner.observe(model,tmp_path)
        with pytest.raises(Exception):model.invoke([HumanMessage('test')])
    progress=runner.read(tmp_path/'wire/000001_progress.json')
    assert not progress['completed'] and progress['bytes']>0
    assert progress['error_chain'][0]['type']=='RemoteProtocolError'
    assert not (tmp_path/'wire/000001_response.json').exists()
    assert (tmp_path/'wire/000001_headers.json').exists()


def test_transport_causes_are_preserved_and_credentials_redacted():
    spec=importlib.util.spec_from_file_location('error_chain_runner',Path(__file__).parents[1]/'scripts/run_resilient_training.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    inner=ConnectionError('private-token socket closed')
    outer=RuntimeError('Connection error')
    outer.__cause__=inner
    chain=runner.transport_error_details(outer,['private-token'])
    assert [x['type'] for x in chain]==['RuntimeError','ConnectionError']
    assert 'private-token' not in json.dumps(chain)


@pytest.mark.parametrize('tail',[
    'data: {"error":{"code":"upstream_stream_truncated","message":"Upstream ended","type":"upstream_error"}}\n\n',
    '',
])
def test_partial_valid_tool_json_is_never_returned_as_a_completed_result(tmp_path,tail):
    import httpx
    from cortex.infrastructure.llm.stream_audit import UpstreamResponseIncomplete
    spec=importlib.util.spec_from_file_location('incomplete_stream_runner',Path(__file__).parents[1]/'scripts/run_resilient_training.py')
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    chunk={'id':'partial','choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0,'id':'proposal','type':'function','function':{'name':'submit_changes','arguments':'{"patch":{"explanation":"no change"}}'}}]},'finish_reason':None}]}
    text='data: '+json.dumps(chunk)+'\n\n'+tail
    with httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,headers={'content-type':'text/event-stream'},stream=httpx.ByteStream(text.encode())))) as client:
        model=DeepSeekChatOpenAI(model='deepseek-v4-pro',api_key='offline-secret',http_client=client,streaming=True,max_retries=0)
        runner.observe(model,tmp_path)
        with pytest.raises(Exception):model.invoke([HumanMessage('test')])
    progress=runner.read(tmp_path/'wire/000001_progress.json')
    assert progress['model_complete'] is False
    assert progress['error_chain'][0]['type']=='UpstreamResponseIncomplete'
    assert (tmp_path/'wire/000001_response.json').exists()
