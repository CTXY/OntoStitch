import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cortex.capabilities.online_context.discovery import intra_batches as module
from cortex.capabilities.online_context.discovery.models import IntraNodeOutput


def payload():
    return {'question':'List a','initial_sql':'SELECT a FROM t',
            'retrieved_graph_state':{'graph':{'nodes':[{'id':'a'},{'id':'b'}],'edges':[]}},
            'join_candidates':[{'edge_ids':['join']}],'signal_node_ids':['a','b']}


def output(signal):
    return IntraNodeOutput.model_validate({'assessments':[{'signal_node_id':signal,
        'status':'confirmed','slot_ids':['same_slot'],'reason':'local alternatives'}],
        'slots':[{'slot_id':'same_slot','scope':'binding','decision_question':'Which binding?',
        'candidates':[{'candidate_id':'c1','statement':'Use a','graph_refs':['a'],
            'evidence_assessment':{'source_types':['ontology_asset'],'applicability':'applicable','explanation':'graph'}},
            {'candidate_id':'c2','statement':'Use b','graph_refs':['b'],
            'evidence_assessment':{'source_types':['ontology_asset'],'applicability':'applicable','explanation':'graph'}}]}]})


def test_batches_retain_all_context_and_namespace_colliding_slot_ids(tmp_path,monkeypatch):
    original=payload();before=deepcopy(original);seen=[]
    async def extract(**kwargs):
        seen.append(kwargs['human_payload']);result=output(kwargs['human_payload']['signal_node_ids'][0])
        kwargs['output_validator'](result);return result
    monkeypatch.setattr(module,'run_llm_extraction',extract)
    result=asyncio.run(module.extract_intra_batches(llm=SimpleNamespace(model_name='offline'),
        prompt='same contract',human_payload=original,batch_size=1,output_validator=lambda _:None,checkpoint_root=tmp_path))
    assert original==before and len(seen)==2
    assert all(p['retrieved_graph_state']==original['retrieved_graph_state'] and p['join_candidates']==original['join_candidates'] for p in seen)
    assert all(list(p)[-1]=='signal_node_ids' for p in seen)
    slot_ids={s.slot_id for s in result.slots};assert len(slot_ids)==2
    assert {a.signal_node_id for a in result.assessments}=={'a','b'}
    assert all(set(a.slot_ids)<=slot_ids for a in result.assessments)


def test_resume_reuses_validated_batches_after_a_later_failure(tmp_path,monkeypatch):
    seen=[];failed=False
    async def extract(**kwargs):
        nonlocal failed
        signal=kwargs['human_payload']['signal_node_ids'][0];seen.append(signal)
        if signal=='b' and not failed:failed=True;raise RuntimeError('provider unavailable')
        return output(signal)
    monkeypatch.setattr(module,'run_llm_extraction',extract)
    args=dict(llm=SimpleNamespace(model_name='offline'),prompt='contract',human_payload=payload(),
        batch_size=1,output_validator=lambda _:None,checkpoint_root=tmp_path)
    with pytest.raises(RuntimeError):asyncio.run(module.extract_intra_batches(**args))
    result=asyncio.run(module.extract_intra_batches(**args))
    assert seen==['a','b','b'] and len(result.assessments)==2


def test_invalid_batch_coverage_is_never_checkpointed(tmp_path,monkeypatch):
    async def extract(**kwargs):return IntraNodeOutput(assessments=(),slots=())
    monkeypatch.setattr(module,'run_llm_extraction',extract)
    with pytest.raises(ValueError,match='exactly the requested'):
        asyncio.run(module.extract_intra_batches(llm=SimpleNamespace(),prompt='contract',human_payload=payload(),
            batch_size=1,output_validator=lambda _:None,checkpoint_root=tmp_path))
    assert not list(tmp_path.glob('*.json'))
