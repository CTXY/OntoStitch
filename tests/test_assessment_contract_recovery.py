"""All actionable contract errors are returned together, without accepting defects."""

from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage

from assessment_fixtures import response_for_payload
from conftest import make_rule
from test_agent_feedback import payload_for, write
from test_agent_runtime import ScriptModel, session_for
from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.training.assessment_models import AssessmentFeedback
from cortex.capabilities.sql_training.training.feedback import AssessmentOutputInvalid, assess_payload, assessment_messages


def two_units(case_graph):
    session = session_for(case_graph)
    before = session.graph.accepted
    write(session, "one")
    write(session, "two")
    groups = [ChangeGroup(group_id="joint", member_ids=[make_rule("one").id, make_rule("two").id])]
    payload = payload_for(session, before, groups)
    return payload, groups


def test_removed_application_labels_are_rejected(case_graph):
    payload, _ = two_units(case_graph)
    data=response_for_payload(payload)
    for row in data["unit_results"]: row["application"]="applied"
    with pytest.raises(ValueError, match="Extra inputs"):
        AssessmentFeedback.model_validate(data)


def test_all_incomplete_quality_checks_are_reported(case_graph):
    payload, _ = two_units(case_graph);data=response_for_payload(payload)
    for row in data["unit_results"]: row["quality_checks"].pop(next(iter(row["quality_checks"])))
    with pytest.raises(ValueError) as error: AssessmentFeedback.model_validate(data).validate_evidence(payload)
    for row in data["unit_results"]: assert f"unit_results[{row['unit_id']}].quality_checks" in str(error.value)


def test_one_format_correction_covers_all_units_without_another_trial(case_graph):
    payload, groups=two_units(case_graph);valid=response_for_payload(payload);invalid=deepcopy(valid)
    for row in invalid["unit_results"]: row["quality_checks"].pop(next(iter(row["quality_checks"])))
    def correction(messages):
        for row in invalid["unit_results"]: assert row["unit_id"] in messages[-1].content
        supplied=json.loads(messages[1].content)
        assert supplied == json.loads(json.dumps(payload))
        assert all("application_options" not in u for u in supplied["units"])
        return AIMessage(content=json.dumps(valid))
    model=ScriptModel([AIMessage(content=json.dumps(invalid)),correction])
    result=assess_payload(model,payload=payload,groups=groups,context_limit=64000)
    assert len(model._calls)==2 and result.acceptable(groups)


def test_exhausted_format_recovery_is_not_a_transient_network_failure(case_graph):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location("assessment_recovery_runner",Path(__file__).parents[1]/"scripts/run_resilient_training.py")
    runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
    payload,groups=two_units(case_graph);invalid=response_for_payload(payload)
    for row in invalid["unit_results"]: row["quality_checks"].pop(next(iter(row["quality_checks"])))
    model=ScriptModel([AIMessage(content=json.dumps(invalid))]*2)
    with pytest.raises(AssessmentOutputInvalid) as error: assess_payload(model,payload=payload,groups=groups,context_limit=64000)
    assert len(model._calls)==2 and not runner.transient(error.value) and not runner.transient(str(error.value))


@pytest.mark.parametrize("checks,expected", [({"one": "valid"}, "valid"),
    ({"one": "valid", "two": "inconclusive"}, "inconclusive"),
    ({"one": "invalid", "two": "inconclusive"}, "invalid")])
def test_missing_summary_is_derived_without_changing_individual_judgments(checks, expected):
    from cortex.capabilities.sql_training.training.assessment_models import UnitResult
    data = {"unit_id": "concept.example", "quality_checks": checks, "feedback": []}
    result = UnitResult.model_validate(data)
    assert result.quality == expected
    assert result.quality_checks == checks
    assert "quality" not in data


def test_runtime_schema_omits_summary_but_still_requires_all_checks(case_graph):
    payload, groups = two_units(case_graph)
    schema = json.loads(assessment_messages(payload)[0].content.split("Response schema:\n", 1)[1])
    assert "quality" not in schema["$defs"]["UnitResult"]["properties"]
    data = response_for_payload(payload)
    for row in data["unit_results"]:
        row.pop("quality")
    result = AssessmentFeedback.model_validate(data)
    result.validate_evidence(payload)
    assert result.acceptable(groups)
    result.unit_results[0].quality_checks.pop(next(iter(result.unit_results[0].quality_checks)))
    with pytest.raises(ValueError, match="quality_checks"):
        result.validate_evidence(payload)


def test_feedback_schema_lists_graph_locations_and_never_target_ids(case_graph):
    from cortex.capabilities.sql_training.training.assessment_models import feedback_locations
    payload, _ = two_units(case_graph)
    payload['fixed_targets'] = [{'target_id': 'E1', 'instruction': 'Investigate the unit'}]
    schema = json.loads(assessment_messages(payload)[0].content.split('Response schema:\n', 1)[1])
    locations = schema['$defs']['QualityFeedback']['properties']['location']['enum']
    assert set(locations) == feedback_locations(payload)
    assert 'E1' not in locations
    assert all(unit['unit_id'] in locations for unit in payload['units'])


def test_derived_summary_does_not_erase_quality_feedback_or_accept_a_defect(case_graph):
    payload, groups = two_units(case_graph)
    data = response_for_payload(payload)
    for row in data["unit_results"]:
        row.pop("quality")
    data["unit_results"][0]["feedback"] = [{"kind": "quality", "location": data["unit_results"][0]["unit_id"],
        "gap": "The operand scope is unsupported.", "required_change": "Correct the operand scope.",
        "preserve": "Preserve the supported calculation."}]
    result = AssessmentFeedback.model_validate(data)
    result.validate_evidence(payload)
    assert not result.acceptable(groups)
    assert result.accepted_members(groups) == []


def test_legacy_inconsistent_summary_is_not_silently_rewritten(case_graph):
    payload, _ = two_units(case_graph)
    data = response_for_payload(payload)
    data["unit_results"][0]["quality"] = "invalid"
    result = AssessmentFeedback.model_validate(data)
    with pytest.raises(ValueError, match="overall quality must agree"):
        result.validate_evidence(payload)
