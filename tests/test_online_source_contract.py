from types import SimpleNamespace as N
import pytest
from cortex.capabilities.online_context.discovery.contracts import validate_initial_sql_sources, validate_filter_basis


def option(id, sources):
    return N(option_id=id, candidate_sources=sources, evidence_assessment=N(source_types=sources))


def test_one_sql_candidate_may_also_have_graph_support():
    validate_initial_sql_sources([N(choice_point_id='P', options=[option('A', ['initial_sql','ontology_asset']),option('B',['ontology_asset'])])])


def test_two_sql_candidates_are_rejected_without_selecting_one():
    point=N(choice_point_id='P',options=[option('A',['initial_sql']),option('B',['initial_sql'])])
    with pytest.raises(ValueError,match='at most one'):
        validate_initial_sql_sources([point])
    assert len(point.options)==2


def test_zero_sql_candidates_remain_valid():
    validate_initial_sql_sources([N(choice_point_id='P',options=[option('A',['ontology_asset']),option('B',['database_schema'])])])


@pytest.mark.parametrize('change',[{'success':False},{'choice_point_id':'other'},{'result':{'truncated':True}},{'result':{'values_truncated':True}}])
def test_equivalence_rejects_unusable_observations(change):
    obs=dict(probe_id='probe:1',choice_point_id='P',success=True,result={});obs.update(change)
    with pytest.raises(ValueError):validate_filter_basis('database_equivalence','See probe:1','P',[obs])


def test_equivalence_needs_existing_probe_id_in_reason():
    with pytest.raises(ValueError):validate_filter_basis('database_equivalence','Usually stable','P',[])
    with pytest.raises(ValueError):validate_filter_basis('database_equivalence','See probe:99','P',[])
    validate_filter_basis('database_equivalence','probe:1 establishes the stated property','P',[dict(probe_id='probe:1',choice_point_id='P',success=True,result={})])


def test_explicit_intent_does_not_need_database_query():
    validate_filter_basis('explicit_intent','Question specifies the direction','P',[])


def test_raw_drafts_also_count_sql_fragment_sources():
    point=N(candidates=[N(sql_fragment="SELECT a"),N(sql_fragment="SELECT b")])
    with pytest.raises(ValueError,match="at most one"):validate_initial_sql_sources([point])


def test_single_json_fence_with_explanation_is_parsed():
    from pydantic import BaseModel
    from cortex.infrastructure.llm.parsing import _parse_output
    class Output(BaseModel):
        value: int
    assert _parse_output('I corrected the field.\n```json\n{"value": 2}\n```', Output).value==2
    with pytest.raises(ValueError):
        _parse_output('```json\n{"value": 1}\n```\n```json\n{"value": 2}\n```', Output)
    with pytest.raises(ValueError):
        _parse_output('Reason\n```json\n{"value": "invalid"}\n```', Output)
