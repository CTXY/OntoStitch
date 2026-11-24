import pytest
from cortex.capabilities.online_context.discovery.sql_fragments import sql_fragments,citation_schema
from cortex.capabilities.online_context.discovery.models import CandidateDraft


def test_exact_sql_handles_resolve_and_unknown_is_rejected():
    sql='SELECT COUNT(a.id) AS n FROM atom a';refs=sql_fragments(sql)
    base={'candidate_id':'c','statement':'Count rows','initial_sql_refs':['S1'],'evidence_assessment':{'source_types':['initial_sql'],'applicability':'applicable','explanation':'The actual count.'}}
    c=CandidateDraft.model_validate(base,context={'sql_fragments':refs})
    assert c.initial_sql_evidence in sql and c.initial_sql_evidence=='COUNT(a.id)'
    with pytest.raises(ValueError):CandidateDraft.model_validate({**base,'initial_sql_refs':['BAD']},context={'sql_fragments':refs})
    with pytest.raises(ValueError):CandidateDraft.model_validate({**base,'initial_sql_evidence':'COUNT(id)'},context={'sql_fragments':refs})


def test_public_schema_does_not_ask_to_copy_sql():
    import json
    s=json.dumps(citation_schema(CandidateDraft));assert 'initial_sql_evidence' not in s and 'initial_sql_refs' in s


def test_guidance_short_rule_reference_restores_exact_action():
    import asyncio,json
    from types import SimpleNamespace
    from cortex.capabilities.online_context.output.guidance import GuidanceAgent
    class Fake:
        async def ainvoke(self,messages):
            payload=json.loads(messages[1]['content']);assert payload['rule_candidates'][0]['rule_id']=='R1'
            return SimpleNamespace(content=json.dumps({'rule_guidance':[{'rule_id':'R1','include':True,'reason':'Applies.'}],'guidance_by_slot':[]}))
    value=asyncio.run(GuidanceAgent(Fake()).synthesize(question='Q',choice_points=(),answers=(),rule_candidates=({'rule_id':'long-original-id','action':'Exact action.'},)))
    assert value.rule_guidance[0].rule_id=='long-original-id' and value.rule_guidance[0].instruction=='Exact action.'
