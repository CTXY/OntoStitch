"""Semantic failures cannot be hidden by SQL success or an overall valid label."""

import pytest
from assessment_fixtures import response_for_payload
from test_assessment_views import grouped_payload

from cortex.capabilities.sql_training.training.assessment_models import (
    AssessmentFeedback,
)


@pytest.mark.parametrize("check", ["decision_value", "abstraction", "atomicity", "condition_action", "graph_consistency"])
def test_rule_check_failure_blocks_an_applied_unit(case_graph, check):
    payload, groups = grouped_payload(case_graph)
    data = response_for_payload(payload)
    row = data["unit_results"][0]
    assert row["application"] == "applied"
    row["quality_checks"][check] = "invalid"
    with pytest.raises(ValueError, match="overall quality"):
        AssessmentFeedback.model_validate(data).validate_evidence(payload)
    row["quality"] = "invalid"
    row["feedback"] = [{
        "kind": "quality", "location": row["unit_id"],
        "gap": f"The {check} requirement is not satisfied.",
        "required_change": "Preserve the operation while correcting the failed requirement.",
        "preserve": "The evidenced SQL behavior.",
    }]
    assessment = AssessmentFeedback.model_validate(data)
    assessment.validate_evidence(payload)
    assert assessment.accepted_members(groups) == []


@pytest.mark.parametrize("alteration", ["missing", "unknown"])
def test_quality_check_inventory_is_not_optional(case_graph, alteration):
    payload, _ = grouped_payload(case_graph)
    data = response_for_payload(payload)
    checks = data["unit_results"][0]["quality_checks"]
    if alteration == "missing":
        checks.pop("abstraction")
    else:
        checks["sql_is_correct"] = "valid"
    with pytest.raises(ValueError, match="exactly the unit's quality_requirements"):
        AssessmentFeedback.model_validate(data).validate_evidence(payload)


def test_relation_requires_predicate_and_endpoint_review(case_graph):
    from test_agent_feedback import payload_for
    from test_semantic_assessment import semantic_payload

    from cortex.capabilities.sql_training.graph.mutations import ChangeGroup

    session, before, _, _ = semantic_payload(case_graph, "concept")
    from cortex.schema import GraphEdge
    edge = GraphEdge(id="edge.reviewed_binding", edge_type="grounded_on", from_node_id="concept.order", to_node_id="table.orders", properties={"description": "The orders table stores the records represented by this meaning."})
    trial = session.graph.save(session.graph.working.assets, parent_revision=session.graph.working_revision, explicit_edges=[edge])
    session.graph.restore(trial.revision, before.revision)
    payload = payload_for(session, before, [ChangeGroup(group_id="edge", member_ids=[edge.id])])
    unit = next(u for u in payload["units"] if u["unit_id"] == edge.id)
    assert unit["candidate"]["properties"]["description"] == edge.properties["description"]
    assert not any(e["id"] == edge.id for e in payload["related_knowledge"]["edges"])
    assert set(unit["quality_requirements"]) == {"endpoint_meaning", "predicate_precision", "scope", "support", "graph_consistency", "assertion_nonredundancy"}
    # Check the actual assessor payload and authoring envelope, not only a guide.
    from cortex.capabilities.sql_training.learning.inputs import ontology_contract
    from cortex.schema import load_root_schema

    for guidance in (
        payload["quality_rubric"]["predicate_precision"],
        ontology_contract(load_root_schema())["custom_relations"],
    ):
        assert "has_attribute is an abstract base, never an actual edge predicate" in guidance
        assert "investigate or omit the unsupported edge" in guidance
        assert "has_attribute is a fallback" not in guidance


def test_proved_redundancy_blocks_relation_acceptance(case_graph):
    from test_agent_feedback import payload_for
    from test_agent_runtime import session_for
    from test_explicit_relations import edge, seed_pair, write

    from cortex.capabilities.sql_training.graph.mutations import ChangeGroup

    store, tool = seed_pair(case_graph)
    before = store.accepted
    assert write(tool, store, "hierarchy", edge_operations=[{"operation": "create_edge", "edge": edge()}])["status"] == "ok"
    groups = [ChangeGroup(group_id="relation", member_ids=["edge.hierarchy"])]
    payload = payload_for(session_for(case_graph), before, groups)
    data = response_for_payload(payload)
    row = data["unit_results"][0]
    row["quality_checks"]["assertion_nonredundancy"] = "invalid"
    row["quality"] = "invalid"
    row["feedback"] = [{
        "kind": "quality", "location": row["unit_id"],
        "gap": "This test assessor reports a proved duplicate within the reviewed scope.",
        "required_change": "Resolve the duplicate without losing the supported assertion or its evidence.",
        "preserve": "The supported hierarchy and scope.",
    }]
    result = AssessmentFeedback.model_validate(data)
    result.validate_evidence(payload)
    assert result.accepted_members(groups) == []


def test_missing_relation_contract_cannot_pass_on_familiar_predicate_name(case_graph):
    from test_agent_feedback import payload_for
    from test_agent_runtime import session_for
    from test_explicit_relations import edge, seed_pair, write

    from cortex.capabilities.sql_training.graph.mutations import ChangeGroup

    store, tool = seed_pair(case_graph)
    before = store.accepted
    assert write(tool, store, "hierarchy", edge_operations=[{"operation": "create_edge", "edge": edge()}])["status"] == "ok"
    groups = [ChangeGroup(group_id="relation", member_ids=["edge.hierarchy"])]
    payload = payload_for(session_for(case_graph), before, groups)
    payload["related_knowledge"]["relation_definitions"] = []
    data = response_for_payload(payload)
    with pytest.raises(ValueError, match="without a supplied contract"):
        AssessmentFeedback.model_validate(data).validate_evidence(payload)
    row = data["unit_results"][0]
    row["quality_checks"]["predicate_precision"] = "inconclusive"
    row["quality"] = "inconclusive"
    row["feedback"] = [{
        "kind": "quality", "location": row["unit_id"],
        "gap": "No relation contract was supplied for is_a.",
        "required_change": "Supply the saved relation contract before judging its scope or inferences.",
        "preserve": "The existing assertion until its meaning can be assessed.",
    }]
    result = AssessmentFeedback.model_validate(data)
    result.validate_evidence(payload)
    assert result.accepted_members(groups) == []


def test_assessment_guidance_and_rubric_keep_consumer_required_materialization():
    from cortex.capabilities.sql_training.llm.prompts import load_prompt
    from cortex.capabilities.sql_training.training.quality_contract import (
        QUALITY_CRITERIA,
    )

    criterion = QUALITY_CRITERIA["assertion_nonredundancy"]
    prompt = load_prompt("learning_assessment")
    assert "retain a materialized assertion" in criterion
    assert "cannot recover the inference" in criterion
    assert "do not fail this check" in criterion
    assert "Direct formula operands and catalog ownership" in criterion
    assert "Retain a materialized assertion" in prompt
    assert "must not make assertion_nonredundancy invalid or inconclusive" in prompt
    assert "must not produce corrective feedback demanding deletion" in prompt
    assert "proof and safe consolidation are both established" in prompt



def metric_review_payload(case_graph):
    from test_agent_feedback import payload_for
    from test_agent_runtime import session_for
    from test_metric_contract import properties

    from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
    from cortex.capabilities.sql_training.models import NodeAsset
    from cortex.schema import GraphEdge

    session = session_for(case_graph)
    before = session.graph.accepted
    metric = NodeAsset.model_validate({
        "id": "metric.request_aggregate", "node_type": "metric",
        "name": "Requested order aggregate", "properties": properties(),
    })
    operand = GraphEdge(
        id="edge.aggregate_input", edge_type="grounded_on",
        from_node_id=metric.id, to_node_id="column.orders.amount",
        properties={"description": "The stored amount is the direct formula input."},
    )
    trial = session.graph.save([*before.assets, metric], parent_revision=before.revision,
                               explicit_edges=[operand])
    session.graph.restore(trial.revision, before.revision)
    groups = [ChangeGroup(group_id="metric", member_ids=[metric.id])]
    validation = {"attempts": [{
        "attempt": 1, "sql": "SELECT SUM(amount) FROM orders", "correct": True,
        "selected_ids": [metric.id], "context_ids": [a.id for a in trial.assets],
        "context_edge_ids": [operand.id],
    }]}
    return payload_for(session, before, groups, validation=validation), groups


@pytest.mark.parametrize("minimality", ["invalid", "inconclusive"])
def test_metric_minimality_blocks_acceptance_despite_successful_sql(case_graph, minimality):
    payload, groups = metric_review_payload(case_graph)
    assert "metric_minimality" in payload["units"][0]["quality_requirements"]
    assert payload["quality_rubric"]["metric_minimality"]
    data = response_for_payload(payload)
    row = data["unit_results"][0]
    assert row["application"] == "applied"
    row["quality_checks"]["metric_minimality"] = minimality
    # SQL success and an overall-valid label cannot override this separate check.
    with pytest.raises(ValueError, match="overall quality"):
        AssessmentFeedback.model_validate(data).validate_evidence(payload)
    row["quality"] = minimality
    row["feedback"] = [{
        "kind": "quality", "location": row["unit_id"],
        "gap": "Only a requested aggregate over stored amounts is established; no independent business calculation is evidenced.",
        "required_change": "Reuse the stored-amount Concept and query operation unless evidence supports an independently defined Metric.",
        "preserve": "The stored amount binding and correct requested aggregate behavior.",
    }]
    result = AssessmentFeedback.model_validate(data)
    result.validate_evidence(payload)
    assert result.accepted_members(groups) == []


def test_metric_minimality_cannot_be_omitted_from_assessment(case_graph):
    payload, _ = metric_review_payload(case_graph)
    data = response_for_payload(payload)
    del data["unit_results"][0]["quality_checks"]["metric_minimality"]
    with pytest.raises(ValueError, match="exactly the unit's quality_requirements"):
        AssessmentFeedback.model_validate(data).validate_evidence(payload)
