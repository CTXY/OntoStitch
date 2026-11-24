"""The first-batch worker has a hard review boundary and English Identity tools."""
import json
from pathlib import Path
import re
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import run_bird_first_batch_review as runner
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.identity.guidance import build_identity_guidance
from cortex.schema import GraphState, GraphSpace, load_root_schema


def test_completed_batch_cannot_start_a_second_batch(tmp_path,monkeypatch):
    database='toxicology'
    runner.write(tmp_path/'plan.json',{'databases':{database:{'case_ids':list(range(10))}}})
    runner.write(tmp_path/database/'status.json',{'status':'awaiting_user_review'})
    monkeypatch.setattr(runner,'configured_model',lambda *args:pytest.fail('Review gate must prevent every new model call'))
    runner.run_worker(tmp_path,database)
    assert runner.read(tmp_path/database/'status.json')['status']=='awaiting_user_review'


def test_worker_rejects_more_than_ten_cases_before_reading_inputs(tmp_path,monkeypatch):
    database='toxicology'
    runner.write(tmp_path/'plan.json',{'databases':{database:{'case_ids':list(range(11))}},'max_batches_per_database':1})
    runner.write(tmp_path/database/'status.json',{'status':'prepared'})
    monkeypatch.setattr(runner,'configured_model',lambda *args:pytest.fail('Invalid batch must not reach API'))
    with pytest.raises(AssertionError):runner.run_worker(tmp_path,database)


@pytest.mark.parametrize('topic',['concept','metric','generation_rule','event','relations','conflicts','operations'])
def test_identity_guidance_uses_active_english_contracts(topic):
    empty=GraphState(graph=GraphSpace())
    context=IntegrationContext(empty,empty,load_root_schema())
    result=build_identity_guidance(context).invoke({'topic':topic})
    assert 'guidance' in result
    assert not re.search('[\u4e00-\u9fff]',json.dumps(result,ensure_ascii=False))
    assert 'set_conflict' not in result['guidance']


def test_physical_value_alias_differences_preserve_synonyms_and_strict_facts():
    from cortex.schema import GraphNode,SourceReference
    from cortex.capabilities.sql_training.physical_catalog import merge_physical_node
    first=GraphNode(id='dimension_value.bond.bond_type.-',node_type='dimension_value',name='-',
                    properties={'column_ref':'column.bond.bond_type','value':'-'},
                    sources=(SourceReference(source_id='case197'),))
    other=first.model_copy(update={'aliases':['single bond'],'sources':(SourceReference(source_id='case198'),)})
    merged=merge_physical_node(first,other)
    assert merged.aliases==['single bond']
    assert set(merged.sources)==set(first.sources)|set(other.sources)
    assert merged.properties==first.properties
    with pytest.raises(ValueError,match='Conflicting physical catalog content'):
        merge_physical_node(first,other.model_copy(update={'properties':{**other.properties,'value':'='}}))
