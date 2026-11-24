"""Fixture provenance and request-language guards for the bounded live pipeline."""
import importlib.util
from pathlib import Path
import sys

import pytest
from langchain_core.messages import HumanMessage

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
spec=importlib.util.spec_from_file_location('pipeline_probe',SCRIPTS/'run_concept_metric_pipeline_probe.py')
probe=importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_real_fixture_includes_both_concept_outgoing_relations_and_metric_inputs(tmp_path):
    before,evidence,hashes=probe.prepare(tmp_path)
    assert len(evidence)==4 and len(hashes)==3
    for case in ('266','204'):
        concept=f'case{case}::{probe.C}'
        metric=f'case{case}::{probe.M}'
        assert {e.edge_type for e in before.graph.edges if e.from_node_id==concept}=={'grounded_on','is_a'}
        deps=[e for e in before.graph.edges if e.from_node_id==metric]
        assert len(deps)==1 and deps[0].to_node_id==concept
    assert all(probe.digest(p)==h for p,h in hashes.items())


def test_non_english_request_is_rejected_before_api(tmp_path):
    class Model:
        def invoke(self,*args,**kwargs):
            raise AssertionError('The API must not be reached')
    guard=probe.EnglishGuard(Model(),tmp_path/'audit.jsonl')
    with pytest.raises(ValueError,match='English prompt guard'):
        guard.invoke([HumanMessage('请修改图')])
    assert (tmp_path/'english_guard_failure.json').exists()


def test_every_runtime_system_prompt_uses_english():
    from cortex.capabilities.graph_merge.llm.node_contract import node_system, NODE_TYPES
    from cortex.capabilities.graph_merge.llm.reconciliation_prompts import reconciliation_system, SIGNALS
    import re
    from importlib.resources import files
    texts=[node_system(role,kind) for role in ('writer','assessment') for kind in NODE_TYPES]
    texts.extend(reconciliation_system(role,SIGNALS) for role in ('writer','assessment'))
    texts.append(files('cortex.capabilities.graph_merge.llm.prompts').joinpath('relation_merger/writer.en.md').read_text())
    assert not any(re.search('[\u4e00-\u9fff]',text) for text in texts)
    assert all('alternativ' in text.lower() for text in texts)


def test_english_catalog_projection_keeps_schema_structure_and_constraints():
    from cortex.capabilities.graph_merge.llm.prompt_language import english_contract
    from cortex.schema import load_root_schema
    original=load_root_schema().model_dump(mode='json')
    translated=english_contract(original)
    def compare(left,right,path=()):
        if isinstance(left,dict):
            assert left.keys()==right.keys()
            for key in left:compare(left[key],right[key],(*path,key))
        elif isinstance(left,list):
            assert len(left)==len(right)
            for a,b in zip(left,right):compare(a,b,path)
        elif left!=right:
            assert path[-1] in {'name','description','direction_semantics','inclusion_criteria',
                                'exclusion_criteria','evidence_requirement'}
    compare(original,translated)
    # Translation is explicitly limited to known prose, never an instruction
    # to translate arbitrary data or change unknown authored contract text.
    assert english_contract({'description':'Custom authored wording','id':'node-A'})=={
        'description':'Custom authored wording','id':'node-A'}
