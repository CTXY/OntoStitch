"""A malformed relevance partition is repaired, never defaulted by code."""
import asyncio
from types import SimpleNamespace
import pytest
from cortex.capabilities.retrieve_db.retrieval import selection


@pytest.mark.parametrize('repair_succeeds',[True,False])
def test_repair_missing_document_once_with_explicit_handle_contract(monkeypatch,repair_succeeds):
    requests=[]
    async def invoke(**kwargs):
        requests.append(dict(kwargs['human_payload']))
        rows=[{'document_id':'d0001','status':'relevant','reason':'Matches the requested meaning.'}]
        if len(requests)>1 and repair_succeeds:rows.append({'document_id':'d0002','status':'irrelevant','reason':'Different meaning.'})
        return selection.RelevanceOutput.model_validate({'decisions':rows})
    monkeypatch.setattr(selection,'run_llm_extraction',invoke)
    docs=tuple(SimpleNamespace(document=SimpleNamespace(document_id=k,canonical_node_id=k,node_type='concept',structured_signals={'content':{'name':k}})) for k in ['first','second'])
    async def run():return await selection.LlmRelevanceSelector(None).select(question='A request',initial_sql=None,representations=docs)
    if repair_succeeds:
        result=asyncio.run(run());assert [d.document_id for d in result]==['first','second']
    else:
        with pytest.raises(ValueError,match='cover every input'):asyncio.run(run())
    assert len(requests)==2
    assert requests[1]['format_feedback']['expected_handles']==['d0001','d0002']
