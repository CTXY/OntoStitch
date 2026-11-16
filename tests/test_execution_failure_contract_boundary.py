"""Normalize only inapplicable annotations; preserve errors and update ownership."""
import json
import pytest
from langchain_core.messages import AIMessage
from cortex.capabilities.sql_training.error_analysis.models import RepairGroup
from cortex.capabilities.sql_training.error_analysis.execution_failure import (
    ExecutionFailureAnalysisOutput, extract_execution_failure_analysis, analyze_execution_failure,
)


def group():
    return {'id':'RG1','error_ids':['E1'],'owner':'generation_rule',
            'repair_instruction':'WRONG_BEHAVIOR: Invalid source column. CORRECT_BEHAVIOR: Use the source-local column.',
            'grouping_reason':'irrelevant singleton grouping',
            'grouping_evidence':{'not_a_real_group_field':'ignored only for singleton'}}


def result():
    return {'summary':'Invalid column in generated SQL.', 'confirmed_errors':[{
        'id':'E1','error_key':'source_column','difference_point':'Column belongs to another source.',
        'gold_logic':'Use a source-local column.','predicted_logic':'Use a foreign source column.',
        'repair_type':'operation','execution_error':'no such column','failing_sql_fragment':'x.missing',
    }], 'repair_groups':[group()]}


def test_singleton_normalizes_at_nested_output_boundary_without_changing_repair():
    source=result();parsed=ExecutionFailureAnalysisOutput.model_validate({'result':source})
    g=parsed.result.repair_groups[0]
    assert g.grouping_evidence is None and g.grouping_reason==''
    assert g.repair_instruction==source['repair_groups'][0]['repair_instruction']
    assert source['repair_groups'][0]['grouping_evidence']  # Caller data was not mutated.


def test_multi_error_group_remains_strict():
    source=group();source['error_ids']=['E1','E2']
    with pytest.raises(ValueError):RepairGroup.model_validate(source)


def test_real_extraction_parser_reaches_mapping_before_late_controller_validation():
    class Model:
        def invoke(self,messages):return AIMessage(content=json.dumps({'result':result()}))
    rows=extract_execution_failure_analysis(Model(),{},'Return JSON: {template_output}')
    assert rows[0]['repair_groups'][0]['grouping_evidence'] is None


def test_invalid_update_base_feedback_identifies_field_and_available_ids():
    source=result();source['repair_groups'][0]['base_asset_id']='rule.draft_only'
    seen=[]
    class Provider:
        def extract_assets(self,payload,**kwargs):
            seen.append(payload)
            row=json.loads(json.dumps(source))
            if len(seen)>1:row['repair_groups'][0]['base_asset_id']=None
            return [row]
    parsed=analyze_execution_failure(Provider(),payload={'accepted_working_assets':[]})
    assert parsed.repair_groups[0].base_asset_id is None
    feedback=seen[1]['validation_feedback']['error']
    assert 'repair_groups[0].base_asset_id' in feedback and 'Accepted generation_rule IDs: []' in feedback
    assert 'null' in feedback
