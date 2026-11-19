from concurrent.futures import ThreadPoolExecutor
import json
import multiprocessing
import threading
import time

import httpx
import pytest
from openai import OpenAI

from cortex.infrastructure.llm.request_limit import limit_client_requests, request_lease


def held_request(directory, live, peak, lock):
    with request_lease(directory, 2):
        with lock:
            live.value += 1
            peak.value = max(peak.value, live.value)
        time.sleep(0.15)
        with lock:
            live.value -= 1


def test_limit_is_shared_between_independent_processes(tmp_path):
    context = multiprocessing.get_context('fork')
    live, peak, lock = context.Value('i', 0), context.Value('i', 0), context.Lock()
    processes = [context.Process(target=held_request, args=(tmp_path, live, peak, lock)) for _ in range(5)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert peak.value == 2 and live.value == 0


def test_stream_lease_covers_body_and_exception_releases_slot(tmp_path):
    live, peak, mutex = [0], [0], threading.Lock()

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            with mutex:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            try:
                time.sleep(0.1)
                yield b'complete response'
            finally:
                with mutex:
                    live[0] -= 1

    def handler(request):
        if request.url.path == '/failure':
            raise httpx.ReadError('synthetic transport failure', request=request)
        return httpx.Response(200, stream=Body())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        limit_client_requests(client, tmp_path, 2)
        limit_client_requests(client, tmp_path, 2)
        with pytest.raises(httpx.ReadError):
            client.send(client.build_request('GET', 'https://offline.invalid/failure'), stream=True)

        def fetch(_):
            response = client.send(client.build_request('GET', 'https://offline.invalid/ok'), stream=True)
            assert response.content == b'complete response'

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(fetch, range(5)))
        assert peak[0] == 2 and live[0] == 0


def test_cached_http_client_routes_old_and_replacement_sdk_keys_independently(tmp_path):
    active = {'key-alpha': 0, 'key-beta': 0}
    peak = {'key-alpha': 0, 'key-beta': 0, 'total': 0}
    lock = threading.Lock()
    payload = json.dumps({'id': 'test', 'object': 'chat.completion', 'created': 0, 'model': 'test',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]}).encode()

    class Body(httpx.SyncByteStream):
        def __init__(self, key):
            self.key = key

        def __iter__(self):
            with lock:
                active[self.key] += 1
                peak[self.key] = max(peak[self.key], active[self.key])
                peak['total'] = max(peak['total'], sum(active.values()))
            try:
                time.sleep(0.12)
                yield payload
            finally:
                with lock:
                    active[self.key] -= 1

    def handler(request):
        key = request.headers['authorization'].split(' ', 1)[1]
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body(key))

    with httpx.Client(transport=httpx.MockTransport(handler)) as shared:
        clients = {}
        for key in active:
            limit_client_requests(shared, tmp_path / key, 1, credential=key, base_url='https://offline.invalid/v1')
            clients[key] = OpenAI(api_key=key, base_url='https://offline.invalid/v1', http_client=shared, max_retries=0)
        original_send = shared.send
        limit_client_requests(shared, tmp_path / 'key-beta', 1, credential='key-beta', base_url='https://offline.invalid/v1')
        assert shared.send is original_send

        def invoke(key):
            reply = clients[key].chat.completions.create(model='test', messages=[{'role': 'user', 'content': 'OK'}])
            assert reply.choices[0].message.content == 'ok'

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(invoke, ['key-alpha', 'key-beta'] * 3))
        assert peak == {'key-alpha': 1, 'key-beta': 1, 'total': 2}
        assert not any(active.values())
