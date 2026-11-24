import asyncio
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize('second_success,circuit_open',[(True,False),(False,False),(False,True)])
def test_online_transport_retry_is_bounded_and_uses_same_checkpoint(tmp_path,monkeypatch,second_success,circuit_open):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    module=importlib.import_module('run_guided_remaining')
    run=object.__new__(module.GuidedRun)
    run.root=tmp_path;run.args=SimpleNamespace(retry_blocked=False);run.current={}
    case={'source_id':'1','database_id':'db','question':'List a','evidence':'','sql':'SELECT 1'}
    run.index={'db/1':case};run.graphs={'db':None};run.contexts={'db':()}
    run.parent_protocol={'database_root':str(tmp_path)};run.protocol={'model':'offline'}
    folder=run.folder('db/1');folder.mkdir(parents=True)
    (folder/'execution.json').write_text(json.dumps({'workflow_identity':'identity','run_id':'samecheckpoint'}))
    gate=tmp_path/'gate.json';gate.write_text(json.dumps({'circuit_open':circuit_open}));run.gate=SimpleNamespace(path=gate)
    model=SimpleNamespace(root_client=SimpleNamespace(close=lambda:None),root_async_client=SimpleNamespace(close=AsyncMock()))
    run.config=lambda *a: (None,None,SimpleNamespace())
    run.model=lambda *a:model
    service=SimpleNamespace(_engine=SimpleNamespace(_workflow=SimpleNamespace(identity='identity')))
    monkeypatch.setattr(module,'build_online_context_service',lambda **kwargs:service)
    monkeypatch.setattr(module,'build_simulated_user_agent',lambda **kwargs:None)
    failure={'status':'error','error':'empty provider response','error_code':'provider_empty_completion','retryable':True}
    calls=[];sql_calls=[]
    async def online(*args,**kwargs):
        calls.append(kwargs['run_id'])
        return {'status':'completed','guidance_content':'Use a'} if second_success and len(calls)==2 else failure
    def sql(*args):
        sql_calls.append(args[2]);return {'evaluation':{'exec':1},'error':None}
    monkeypatch.setattr(module,'_online_case',online);monkeypatch.setattr(module,'generate_sql_arm',sql)
    if second_success:
        asyncio.run(run.one('db/1',0));assert sql_calls==['guided']
        assert json.loads((folder/'case_result.json').read_text())['outcome']=='correct'
    else:
        with pytest.raises(module.TransportStopped):asyncio.run(run.one('db/1',0))
        assert not sql_calls and not (folder/'case_result.json').exists()
    assert calls==['samecheckpoint']*(1 if circuit_open else 2)


@pytest.mark.parametrize('artifact_state',['original','seed_snapshot','changed_original','changed_snapshot'])
def test_baseline_preservation_verifies_original_or_exact_seed(tmp_path,monkeypatch,artifact_state):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    module=importlib.import_module('run_guided_remaining')
    run=object.__new__(module.GuidedRun)
    record={'sql':'SELECT 1','evaluation':{'exec':1}}
    original=tmp_path/'historical'/'plain_prediction.json'
    snapshot=tmp_path/'preserved.json'
    data=json.dumps(record).encode()
    digest=hashlib.sha256(data).hexdigest()
    snapshot.write_bytes(data)
    run.index={'db/1':{'plain':record}}
    run.protocol={
        'preserved_baseline_hashes':{str(original):digest},
        'baseline_snapshot_fallbacks':{str(original):{
            'case':'db/1','snapshot':str(snapshot),'snapshot_sha256':digest,
        }},
    }
    if artifact_state in {'original','changed_original'}:
        original.parent.mkdir()
        original.write_bytes(data if artifact_state=='original' else b'{}')
    elif artifact_state=='changed_snapshot':
        snapshot.write_bytes(b'{}')
    if artifact_state.startswith('changed_'):
        with pytest.raises(AssertionError):run.verify_baseline_files()
    else:
        verified=run.verify_baseline_files()
        assert verified['all_baseline_record_values_preserved']
        assert verified['existing_baseline_files_unchanged']==(artifact_state=='original')


def test_worker_config_uses_distinct_keys_and_selected_endpoint(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    module=importlib.import_module('run_guided_remaining')
    from cortex.config.models import LLMConfig
    from cortex.infrastructure.llm.key_slots import read_key_slots
    keys=tmp_path/'keys';keys.write_text('offline-key-one\noffline-key-two\n')
    run=object.__new__(module.GuidedRun);run.root=tmp_path;run.parent=tmp_path;run.parent_protocol={}
    run.provider={'keys_file':str(keys),'base_url':'https://selected.invalid/v1'}
    run.slots=read_key_slots(keys)
    def configure(parent,protocol,key,keyfile,db,sid,folder):
        class Config:
            llm=LLMConfig(model='offline',api_key=key,base_url='https://old.invalid')
            def model_copy(self,*,update):return SimpleNamespace(**update)
        return None,None,Config()
    monkeypatch.setattr(module,'configure',configure)
    configs=[run.config('db/1',i)[2] for i in range(2)]
    assert [c.llm.api_key.get_secret_value() for c in configs]==['offline-key-one','offline-key-two']
    assert all(c.llm.base_url=='https://selected.invalid/v1' for c in configs)


def test_gateway_without_cache_fields_has_unknown_hit_rate(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    module=importlib.import_module('run_guided_remaining')
    run=object.__new__(module.GuidedRun);run.root=tmp_path
    wire=tmp_path/'db/cases/1/wire';wire.mkdir(parents=True)
    (wire/'000001_response.json').write_text(json.dumps({'status':200,'body':{'usage':{'prompt_tokens':20000}}}))
    usage=run.cache_usage()['online']
    assert usage['token_hit_rate'] is None and usage['responses_without_cache_metrics']==1


@pytest.mark.parametrize('failure_kind',['credential','transport'])
def test_one_worker_failure_preserves_case_and_does_not_cancel_other_cases(tmp_path,monkeypatch,failure_kind):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'scripts'))
    module=importlib.import_module('run_guided_remaining')
    run=object.__new__(module.GuidedRun);run.root=tmp_path
    run.args=SimpleNamespace(workers=2);run.protocol={'selected_cases':['db/1','db/2'],'baseline_records_available':0}
    run.disabled_workers=set();run.finished=False;run.stop_reason=None
    for case in run.protocol['selected_cases']:run.folder(case).mkdir(parents=True)
    async def prepare():pass
    run.prepare=prepare;run.verify_baseline_files=lambda:{}
    run.publish=lambda:{'status':'running','guided_evaluated':1,'guided_correct':1}
    completed=[]
    async def one(key,worker):
        if key=='db/1' and (failure_kind=='transport' or worker==0):
            error=module.WorkerKeyUnavailable if failure_kind=='credential' else module.CaseDeferred
            raise error('test service failure')
        completed.append((key,worker))
    run.one=one
    asyncio.run(run.run())
    assert any(case=='db/2' for case,_ in completed)
    if failure_kind=='credential':
        assert ('db/1',1) in completed and run.disabled_workers=={0}
        assert (run.folder('db/1')/'key_reassignment.json').exists()
    else:
        deferred=json.loads((run.folder('db/1')/'deferred.json').read_text())
        assert deferred['checkpoint_preserved'] and deferred['excluded_from_ea']
    assert run.stop_reason is None
