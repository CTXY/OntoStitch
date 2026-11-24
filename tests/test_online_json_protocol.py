import json
from pathlib import Path
import pytest
from cortex.infrastructure.llm.parsing import _parse_json_object
from cortex.capabilities.online_context.discovery.models import IntraNodeOutput,CrossNodeOutput


def test_direct_empty_outputs_and_wrappers():
    assert not _parse_json_object('{"assessments":[],"slots":[]}',IntraNodeOutput).slots
    assert not _parse_json_object('{"slot_augmentations":[],"new_slots":[]}',CrossNodeOutput).new_slots
    with pytest.raises(ValueError,match='wrapper'):
        _parse_json_object('{"result":{"slot_augmentations":[],"new_slots":[]}}',CrossNodeOutput)
    with pytest.raises(ValueError,match='Duplicate'):
        _parse_json_object('{"slots":[],"slots":[],"assessments":[]}',IntraNodeOutput)


def test_real_failures_are_rejected_without_guessing():
    root=Path(__file__).parent/'fixtures/online_json_output_failures'
    for sid,number,model in [('1244','000021',IntraNodeOutput),('1244','000022',IntraNodeOutput),('1311','000005',CrossNodeOutput),('1311','000006',CrossNodeOutput)]:
        raw=json.loads((root/f'{sid}_{number}.json').read_text())['response']
        with pytest.raises(ValueError):_parse_json_object(raw,model)


def test_json_mode_repair_keeps_context_and_wire_contract():
    import asyncio
    from types import SimpleNamespace
    from cortex.infrastructure.llm.parsing import run_llm_extraction
    class Fake:
        def __init__(self): self.messages=[]
        def bind(self,**kwargs):
            assert kwargs=={'response_format':{'type':'json_object'}}
            return self
        async def ainvoke(self,messages):
            self.messages.append(messages)
            return SimpleNamespace(content='{"result":{"assessments":[],"slots":[]}}' if len(self.messages)==1 else '{"assessments":[],"slots":[]}')
    llm=Fake()
    value=asyncio.run(run_llm_extraction(llm=llm,prompt='Return a root JSON object. {template_output}',output_model=IntraNodeOutput,human_payload={'question':'original question'},json_mode=True,repair_with_context=True))
    assert not value.slots and len(llm.messages)==2
    assert llm.messages[1][:2]==llm.messages[0]
    assert 'wrapper' in llm.messages[1][-1]['content']


def test_candidate_unknown_graph_handle_still_rejected():
    from cortex.capabilities.online_context.discovery.models import CandidateDraft
    from cortex.capabilities.online_context.discovery.organizer import _validate_candidate_references
    candidate=CandidateDraft.model_validate({'candidate_id':'c','statement':'Use X','graph_refs':['missing'],
        'evidence_assessment':{'source_types':['ontology_asset'],'applicability':'applicable','explanation':'A claimed rule.'}})
    with pytest.raises(ValueError):_validate_candidate_references(candidate,{'g1'},None)


def test_candidate_input_omits_internal_source_field():
    from cortex.capabilities.online_context.discovery.organizer import _discovery_slot
    value={'options':[{'candidate_sources':['memory_graph'],'graph_refs':['g1'],'evidence_assessment':{'source_types':['ontology_asset']}}]}
    clean=_discovery_slot(value)
    assert clean=={'options':[{'graph_refs':['g1'],'evidence_assessment':{'source_types':['ontology_asset']}}]}
    assert 'candidate_sources' in value['options'][0]
