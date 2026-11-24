import asyncio
import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from cortex.infrastructure.llm.adaptive_limit import (
    AdaptiveRequestGate,
    AdmissionPaused,
    InvalidCompletion,
    install_adaptive_gate,
)


def hold_shared_slot(directory, live, peak, lock):
    gate = AdaptiveRequestGate(directory, max_limit=1)
    token = gate.acquire()
    with lock:
        live.value += 1
        peak.value = max(peak.value, live.value)
    time.sleep(0.08)
    with lock:
        live.value -= 1
    gate.release(token, success=True)


def test_independent_processes_share_one_actual_request_slot(tmp_path):
    ctx = multiprocessing.get_context("fork")
    live, peak, lock = ctx.Value("i", 0), ctx.Value("i", 0), ctx.Lock()
    workers = [
        ctx.Process(target=hold_shared_slot, args=(tmp_path, live, peak, lock))
        for _ in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert peak.value == 1 and live.value == 0


def test_sync_and_async_admission_holds_until_the_entire_body_is_read(tmp_path):
    gate = AdaptiveRequestGate(tmp_path, max_limit=1)
    body = json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": "OK"}}]}
    ).encode()

    class SlowSync(httpx.SyncByteStream):
        def __iter__(self):
            time.sleep(0.06)
            yield body

    class SlowAsync(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.06)
            yield body

    async def run():
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=SlowSync())
            )
        ) as sync:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, stream=SlowAsync())
                )
            ) as async_client:
                install_adaptive_gate(sync, gate)
                install_adaptive_gate(async_client, gate, asynchronous=True)

                def synchronous():
                    return sync.post(
                        "https://offline.invalid",
                        json={"reasoning_effort": "none", "stream": False},
                    )

                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(synchronous)
                    await asyncio.gather(
                        *(
                            async_client.post(
                                "https://offline.invalid",
                                json={"reasoning_effort": "none"},
                            )
                            for _ in range(3)
                        )
                    )
                    assert future.result().status_code == 200
        state = json.loads(gate.path.read_text())
        assert state["completed"] == 4 and state["peak_active"] == 1
        assert state["active"] == {}

    asyncio.run(run())


def test_admission_ramps_after_complete_responses_and_breaks_on_repeated_failure(
    tmp_path,
):
    gate = AdaptiveRequestGate(tmp_path, stages=((2, 0), (2, 0)))
    for _ in range(4):
        token = gate.acquire()
        gate.release(token, success=True)
    assert json.loads(gate.path.read_text())["limit"] == 3
    token = gate.acquire()
    gate.release(token, success=False, detail="HTTP 502")
    state = json.loads(gate.path.read_text())
    assert state["limit"] == 1 and not state["circuit_open"]
    with gate._state() as state:
        state["cooldown_until"] = 0
    token = gate.acquire()
    gate.release(token, success=False, detail="HTTP 502")
    with pytest.raises(AdmissionPaused):
        gate.try_acquire()


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "length", "message": {"content": '{"partial":'}},
        {
            "finish_reason": "stop",
            "message": {"content": "OK", "reasoning_content": "unexpected"},
        },
    ],
)
def test_http_200_is_not_enough_to_raise_admission(tmp_path, choice):
    gate = AdaptiveRequestGate(tmp_path)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"choices": [choice]})
        )
    ) as client:
        install_adaptive_gate(client, gate)
        with pytest.raises(InvalidCompletion):
            client.post("https://offline.invalid", json={"reasoning_effort": "none"})
    state = json.loads(gate.path.read_text())
    assert state["circuit_open"] == (choice['finish_reason'] != 'length')
    assert not state["active"]
    assert state["completed"] == 0


def test_cancelled_async_response_releases_admission(tmp_path):
    gate = AdaptiveRequestGate(tmp_path, max_limit=1)
    reading = asyncio.Event()

    class WaitingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            reading.set()
            await asyncio.sleep(30)
            yield b"not reached"

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=WaitingBody())
            )
        ) as client:
            install_adaptive_gate(client, gate, asynchronous=True)
            task = asyncio.create_task(
                client.post(
                    "https://offline.invalid", json={"reasoning_effort": "none"}
                )
            )
            await asyncio.wait_for(reading.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert json.loads(gate.path.read_text())["active"] == {}
        assert json.loads(gate.path.read_text())["failed"] == 0
        assert json.loads(gate.path.read_text())["cancelled"] == 1

    asyncio.run(run())


def test_one_empty_http_200_backs_off_without_immediately_opening_circuit(tmp_path):
    from cortex.infrastructure.llm.adaptive_limit import EmptyCompletion
    responses=iter([
        {"choices":[{"finish_reason":"stop","message":{"content":""}}]},
        {"choices":[{"finish_reason":"stop","message":{"content":"OK"}}]},
    ])
    gate=AdaptiveRequestGate(tmp_path,max_limit=1)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=next(responses)))) as client:
        install_adaptive_gate(client,gate)
        with pytest.raises(EmptyCompletion):client.post('https://offline.invalid',json={'reasoning_effort':'none'})
        state=json.loads(gate.path.read_text())
        assert state['failed']==1 and not state['circuit_open'] and state['limit']==1
        with gate._state() as state:state['cooldown_until']=0
        assert client.post('https://offline.invalid',json={'reasoning_effort':'none'}).status_code==200
    assert json.loads(gate.path.read_text())['consecutive_failures']==0


def test_short_responses_cannot_unlock_long_request_validation(tmp_path):
    gate=AdaptiveRequestGate(tmp_path,max_limit=2,stages=((2,0),),min_long_completions=2)
    for _ in range(6):
        gate.release(gate.acquire(),success=True,prompt_tokens=100)
    assert json.loads(gate.path.read_text())['limit']==1
    gate.release(gate.acquire(),success=True,prompt_tokens=20000)
    assert json.loads(gate.path.read_text())['limit']==1
    gate.release(gate.acquire(),success=True,prompt_tokens=20000)
    assert json.loads(gate.path.read_text())['limit']==2
    gate.release(gate.acquire(),success=False,detail='HTTP 502')
    state=json.loads(gate.path.read_text())
    assert state['limit']==1 and state['stable_long_completions']==0


def test_quota_failure_stops_admission_immediately(tmp_path):
    gate=AdaptiveRequestGate(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(402,json={'error':{'type':'insufficient_balance_error'}}))) as client:
        install_adaptive_gate(client,gate)
        assert client.post('https://offline.invalid',json={'reasoning_effort':'none'}).status_code==402
    assert json.loads(gate.path.read_text())['circuit_open']


def test_html_502_remains_http_failure_instead_of_json_format_error(tmp_path):
    gate=AdaptiveRequestGate(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(502,text='<html>Bad Gateway</html>'))) as client:
        install_adaptive_gate(client,gate)
        assert client.post('https://offline.invalid',json={'reasoning_effort':'none'}).status_code==502
    state=json.loads(gate.path.read_text())
    assert state['failed']==1 and not state['circuit_open']
    assert state['transitions'][-1]['reason']=='HTTP 502'


def test_gateway_final_answer_can_be_retained_while_mode_deviation_is_audited(tmp_path):
    gate=AdaptiveRequestGate(tmp_path,allow_provider_reasoning=True)
    body={'choices':[{'finish_reason':'stop','message':{'content':'SELECT 1','reasoning_content':'provider ignored requested none'}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=body))) as client:
        install_adaptive_gate(client,gate)
        assert client.post('https://offline.invalid',json={'reasoning_effort':'none'}).status_code==200
    state=json.loads(gate.path.read_text())
    assert state['completed']==1 and state['provider_reasoning_responses']==1 and not state['circuit_open']


@pytest.mark.parametrize('asynchronous',[False,True])
def test_http_retries_repeat_only_same_request_and_are_bounded(tmp_path,monkeypatch,asynchronous):
    from cortex.infrastructure.llm import adaptive_limit as module
    calls=[]
    def transport(request):
        calls.append(request.content)
        return httpx.Response(502,text='upstream timeout')
    gate=AdaptiveRequestGate(tmp_path,cooldown_seconds=0,failure_threshold=99)
    async def no_sleep(_):pass
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    monkeypatch.setattr(module.asyncio,'sleep',no_sleep)
    payload={'reasoning_effort':'none','messages':[{'role':'user','content':'same prompt'}]}
    if asynchronous:
        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                install_adaptive_gate(client,gate,asynchronous=True,max_retries=6)
                assert (await client.post('https://offline.invalid',json=payload)).status_code==502
        asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            install_adaptive_gate(client,gate,max_retries=6)
            assert client.post('https://offline.invalid',json=payload).status_code==502
    assert len(calls)==7 and len(set(calls))==1
    state=json.loads(gate.path.read_text())
    assert state['http_retries']==6 and not state['active']


def test_bad_key_is_disabled_without_stopping_other_keys_or_lowering_admission(tmp_path):
    import hashlib
    from cortex.infrastructure.llm.adaptive_limit import CredentialUnavailable
    gate=AdaptiveRequestGate(tmp_path,max_limit=6,stages=((2,0),)*5,initial_limit=6,min_limit=2,isolate_credentials=True)
    calls=[]
    def transport(request):
        calls.append(request.headers['authorization'])
        return httpx.Response(401,json={'error':'unauthorized'}) if request.headers['authorization']=='Bearer bad-test-key' else httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        install_adaptive_gate(client,gate,max_retries=6)
        assert client.post('https://offline.invalid',headers={'Authorization':'Bearer bad-test-key'},json={'reasoning_effort':'none'}).status_code==401
        with pytest.raises(CredentialUnavailable):client.post('https://offline.invalid',headers={'Authorization':'Bearer bad-test-key'},json={'reasoning_effort':'none'})
        assert client.post('https://offline.invalid',headers={'Authorization':'Bearer good-test-key'},json={'reasoning_effort':'none'}).status_code==200
    state=json.loads(gate.path.read_text())
    assert len(calls)==2 and state['limit']==6 and not state['circuit_open']
    assert hashlib.sha256(b'bad-test-key').hexdigest()[:12] in state['disabled_keys']


def test_high_concurrency_halves_on_failure_and_recovers_after_cooldown(tmp_path):
    gate=AdaptiveRequestGate(tmp_path,max_limit=10,stages=((2,0),)*9,initial_limit=6,min_limit=2,decrease_factor=.5,failure_threshold=2,cooldown_seconds=0,auto_recover_seconds=90)
    gate.release(gate.acquire(),success=False,detail='HTTP 502')
    assert json.loads(gate.path.read_text())['limit']==3
    gate.release(gate.acquire(),success=False,detail='HTTP 502')
    assert gate.try_acquire() is None
    with gate._state() as state:state['cooldown_until']=0
    token=gate.try_acquire();assert token
    assert json.loads(gate.path.read_text())['limit']==2
    gate.release(token,success=True)


@pytest.mark.parametrize('mode,payload,valid',[
    ('explicit_none',{'thinking':{'type':'disabled'}},False),
    ('native_disabled',{'thinking':{'type':'disabled'}},True),
    ('native_disabled',{},False),
    ('native_disabled',{'reasoning_effort':'none','thinking':{'type':'disabled'}},False),
])
def test_wire_mode_is_explicit_and_never_accepts_accidental_parameter_clearing(tmp_path,mode,payload,valid):
    gate=AdaptiveRequestGate(tmp_path,wire_request_mode=mode)
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'OK'}}]}))) as client:
        install_adaptive_gate(client,gate)
        if valid:assert client.post('https://offline.invalid',json=payload).status_code==200
        else:
            with pytest.raises(InvalidCompletion):client.post('https://offline.invalid',json=payload)
