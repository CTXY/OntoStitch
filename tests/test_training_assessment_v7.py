"""Acceptance uses quality, actual injection and related errors, never SQL-use coverage."""
import json

import pytest
from assessment_fixtures import response_for_payload
from test_assessment_views import grouped_payload
from cortex.capabilities.sql_training.training.assessment_models import AssessmentFeedback
from cortex.capabilities.sql_training.training.feedback import assessment_messages


def test_injected_but_unused_knowledge_does_not_require_a_graph_edit(case_graph):
    payload, groups = grouped_payload(case_graph)
    # The fixture supplies three successful SELECT literals and an injected Rule.
    # The independent quality verdict is valid. Its name/action need not appear in SQL.
    result = AssessmentFeedback.model_validate(response_for_payload(payload))
    result.validate_evidence(payload)
    assert result.retrieval_results[0].status == 'injected'
    assert result.unit_results[0].feedback == []
    assert result.acceptable(groups)
    schema = json.loads(assessment_messages(payload)[0].content.split('Response schema:\n')[1])
    assert 'application' not in schema['$defs']['UnitResult']['properties']
    assert 'retrieval_results' not in schema['properties']
    assert all('application_requirement' not in unit for unit in payload['units'])


def test_case_mapping_values_do_not_need_where_filters(case_graph):
    payload, groups = grouped_payload(case_graph)
    for attempt in payload['attempts']:
        attempt['sql'] = "SELECT CASE label WHEN '+' THEN 'YES' WHEN '-' THEN 'NO' END FROM molecule"
    result = AssessmentFeedback.model_validate(response_for_payload(payload))
    result.validate_evidence(payload)
    assert result.acceptable(groups)


def test_target_feedback_schema_cannot_advertise_application_kind(case_graph):
    payload, _ = grouped_payload(case_graph)
    payload['fixed_targets'] = [{'target_id':'E1'}]
    schema = json.loads(assessment_messages(payload)[0].content.split('Response schema:\n')[1])
    assert schema['$defs']['TargetFeedback']['properties']['kind']['const'] == 'target'
    assert 'E1' not in schema['$defs']['TargetFeedback']['properties']['location']['enum']
    data = response_for_payload(payload)
    data['target_results'][0].update(status='unresolved', feedback=[{
        'kind':'application','location':payload['units'][0]['unit_id'],
        'gap':'Output column missing.','required_change':'Restore column.','preserve':'Population.'}])
    with pytest.raises(ValueError):
        AssessmentFeedback.model_validate(data)


def test_saved_injection_cannot_be_forged_by_an_assessment(case_graph):
    payload, _ = grouped_payload(case_graph)
    result = AssessmentFeedback.model_validate(response_for_payload(payload))
    result.validate_evidence(payload)
    saved = result.model_dump(mode='json')
    saved['retrieval_results'][0]['status'] = 'not_injected'
    with pytest.raises(ValueError, match='disagree with recorded injection'):
        AssessmentFeedback.model_validate(saved).validate_evidence(payload)


def test_raw_recall_is_not_mislabeled_as_injection(case_graph):
    payload, groups = grouped_payload(case_graph)
    fact = payload['units'][0]['context_facts'][0]
    fact.update(raw_retrieved=True, selected=False, included_via=[], present_in_context=False)
    result = AssessmentFeedback.model_validate(response_for_payload(payload))
    result.validate_evidence(payload)
    assert result.unit_results[0].quality == 'valid'
    assert result.retrieval_results[0].status == 'not_injected'
    assert not result.acceptable(groups)


def test_sql_error_needs_real_attempt_ids_and_sql_fragment(case_graph):
    payload, _ = grouped_payload(case_graph)
    data = response_for_payload(payload)
    data['unit_results'][0]['feedback'] = [{'kind':'sql_error',
        'location':payload['units'][0]['unit_id'], 'attempt_ids':[99], 'sql_excerpt':'invented SQL',
        'gap':'A purported related error.','required_change':'A purported correction.','preserve':'The valid graph.'}]
    with pytest.raises(ValueError) as error:
        AssessmentFeedback.model_validate(data).validate_evidence(payload)
    assert 'attempt_ids' in str(error.value) and 'sql_excerpt' in str(error.value)
