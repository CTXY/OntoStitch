"""Semantic structure has evidence obligations without invented SQL attribution."""

from copy import deepcopy

import pytest
from assessment_fixtures import response_for_payload
from conftest import make_rule
from test_agent_feedback import payload_for, write
from test_agent_runtime import session_for

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.assessment_inputs import (
    application_requirement,
)
from cortex.capabilities.sql_training.training.assessment_models import (
    AssessmentFeedback,
    LearningFeedback,
)
from cortex.schema import GraphEdge


def semantic_payload(case_graph, kind="event"):
    session = session_for(case_graph)
    before = session.graph.accepted
    node = NodeAsset.model_validate({
        "id": f"{kind}.order" if kind == "concept" else "event.order_placement",
        "node_type": kind,
        "name": "Order" if kind == "concept" else "Order placement",
        "description": "A customer order." if kind == "concept" else "A customer places an order.",
        "properties": {"targets": [["table.orders"]] if kind == "concept" else []},
    })
    trial = session.graph.save([*before.assets, node], parent_revision=before.revision)
    session.graph.restore(trial.revision, before.revision)
    groups = [ChangeGroup(group_id="semantic", member_ids=[node.id])]
    validation = {"attempts": [{"attempt": 1, "sql": "SELECT COUNT(*) FROM orders", "correct": True,
                                 "selected_ids": [], "context_ids": [], "context_edge_ids": []}]}
    payload = payload_for(session, before, groups, validation=validation)
    return session, before, payload, groups


def semantic_response(payload):
    data = response_for_payload(payload)
    for row in data["unit_results"]:
        row.update(application="not_applicable", feedback=[])
    return AssessmentFeedback.model_validate(data)


@pytest.mark.parametrize("kind", ["event"])
def test_event_semantic_units_need_quality_but_not_sql_attribution(case_graph, kind):
    _, _, payload, groups = semantic_payload(case_graph, kind)
    assert "operation_registry" not in payload
    assert payload["units"][0]["application_requirement"] == "semantic"
    assert payload["units"][0]["context_facts"][0]["present_in_context"] is False
    response = semantic_response(payload)
    response.validate_evidence(payload)
    assert response.acceptable(groups)


def test_actual_explicit_business_relation_is_semantic_without_sql_use(case_graph):
    from cortex.capabilities.sql_training.learning.tools import build_tools

    session, before, _, _ = semantic_payload(case_graph, "concept")
    second = NodeAsset.model_validate({"id": "concept.customer_order", "node_type": "concept",
                                      "name": "Customer order", "description": "An order placed by a customer.",
                                      "properties": {"targets": [["column.orders.id"]]}})
    edge = GraphEdge(id="edge.business", edge_type="business_association",
                     from_node_id="concept.order", to_node_id=second.id,
                     properties={"description": "The case identifies these order meanings."})
    tool = next(item for item in build_tools(session) if item.name == "edit_graph")
    result = tool.invoke({
        "edit_id": "business_association", "expected_revision": session.graph.working_revision,
        "upserts": [second.model_dump(mode="json")],
        "relation_contracts": [{
            "id": "business_association", "name": "Business association",
            "description": "An evidenced association between business meanings, without SQL execution semantics.",
            "from_node_types": ["concept"], "to_node_types": ["concept"],
            "direction_semantics": "Source meaning to the associated target meaning.",
            "evidence_requirement": "An explicit association supported by the case.",
        }],
        "edge_operations": [{"operation": "create_edge", "edge": edge.model_dump(mode="json", exclude={"sources", "reference_origin"})}],
    })
    assert result["status"] == "ok", result
    groups = [ChangeGroup(group_id="relation", member_ids=[edge.id])]
    payload = payload_for(session, before, groups, validation={"attempts": [{
        "attempt": 1, "sql": "SELECT COUNT(*) FROM orders", "correct": True,
        "selected_ids": [], "context_ids": [], "context_edge_ids": [],
    }]})
    assert payload["units"][0]["application_requirement"] == "semantic"
    response = semantic_response(payload)
    response.validate_evidence(payload)
    assert response.accepted_members(groups) == [edge.id]


@pytest.mark.parametrize("kind", ["metric", "generation_rule", "dimension_value"])
def test_executable_nodes_cannot_opt_out_via_untrusted_property(kind):
    candidate = {"node_type": kind, "properties": {"application_requirement": "semantic"}}
    assert application_requirement(candidate) == "sql"


def test_anchor_override_is_sql_even_when_its_name_is_custom():
    edge = GraphEdge(id="edge.custom", edge_type="business_association",
                     from_node_id="metric.amount", to_node_id="column.orders.amount",
                     reference_origin="explicit")
    assert application_requirement({"id": edge.id}, relation=edge) == "sql"
    independent = edge.model_copy(update={"reference_origin": None})
    assert application_requirement({"id": edge.id}, relation=independent) == "semantic"


@pytest.mark.parametrize("bound", [False, True])
def test_event_context_is_semantic_but_bound_concept_still_requires_sql(bound):
    properties = {"targets": [["column.orders.id"]]} if bound else {"targets": []}
    assert application_requirement({"node_type": "event", "properties": properties}) == "semantic"
    if bound:
        assert application_requirement({"node_type": "concept", "properties": properties}) == "sql"
    else:
        with pytest.raises(ValueError, match="targets"):
            NodeAsset.model_validate({"id": "concept.unbound", "node_type": "concept", "name": "unbound", "properties": properties})


def test_rule_not_applicable_rejected_by_actual_assessment_payload(case_graph):
    session = session_for(case_graph)
    before = session.graph.accepted
    write(session)
    groups = [ChangeGroup(group_id="rule", member_ids=[make_rule().id])]
    payload = payload_for(session, before, groups)
    assert payload["units"][0]["application_requirement"] == "sql"
    response = semantic_response(payload)
    with pytest.raises(ValueError, match="SQL application is required"):
        response.validate_evidence(payload)


def test_metric_not_applicable_rejected_by_actual_assessment_payload(case_graph):
    from test_metric_contract import properties

    session = session_for(case_graph)
    before = session.graph.accepted
    metric = NodeAsset.model_validate({"id": "metric.total_order_amount", "node_type": "metric",
                                       "name": "Total order amount", "properties": properties()})
    operand = GraphEdge(
        id="edge.metric_amount_input", edge_type="grounded_on",
        from_node_id=metric.id, to_node_id="column.orders.amount",
        properties={"description": "The amount column supplies the values summed by this Metric."},
    )
    trial = session.graph.save([*before.assets, metric], parent_revision=before.revision,
                               explicit_edges=[operand])
    session.graph.restore(trial.revision, before.revision)
    groups = [ChangeGroup(group_id="metric", member_ids=[metric.id])]
    payload = payload_for(session, before, groups)
    assert payload["units"][0]["application_requirement"] == "sql"
    with pytest.raises(ValueError, match="SQL application is required"):
        semantic_response(payload).validate_evidence(payload)


def test_missing_application_policy_fails_closed_for_existing_payloads(case_graph):
    session = session_for(case_graph)
    before = session.graph.accepted
    write(session)
    groups = [ChangeGroup(group_id="rule", member_ids=[make_rule().id])]
    payload = payload_for(session, before, groups)
    del payload["units"][0]["application_requirement"]
    with pytest.raises(ValueError, match="SQL application is required"):
        semantic_response(payload).validate_evidence(payload)


@pytest.mark.parametrize("quality", ["invalid", "inconclusive"])
def test_unsupported_semantic_structure_is_not_accepted(case_graph, quality):
    _, _, payload, groups = semantic_payload(case_graph)
    response = semantic_response(payload)
    response.unit_results[0].quality = quality
    response.unit_results[0].quality_checks = dict.fromkeys(response.unit_results[0].quality_checks, quality)
    with pytest.raises(ValueError, match="quality feedback"):
        response.validate_evidence(payload)
    node_id = payload["units"][0]["unit_id"]
    response.unit_results[0].feedback = [LearningFeedback.model_validate({
        "kind": "quality", "location": node_id, "gap": "The case does not support this business meaning.",
        "required_change": "Remove the unsupported interpretation.", "preserve": "The supported order calculation.",
    })]
    response.validate_evidence(payload)
    assert response.accepted_members(groups) == []


def test_only_deletions_may_have_no_application_contract(case_graph):
    _, _, payload, _ = semantic_payload(case_graph)
    payload = deepcopy(payload)
    payload["units"][0]["application_requirement"] = "none"
    with pytest.raises(ValueError, match="only deleted units"):
        semantic_response(payload).validate_evidence(payload)


def test_assessor_cannot_write_its_own_application_requirement():
    with pytest.raises(ValueError, match="Extra inputs"):
        AssessmentFeedback.model_validate({"unit_results": [{
            "unit_id": "metric.amount", "quality": "valid", "application": "not_applicable",
            "application_requirement": "semantic",
        }], "target_results": []})


def test_concept_representation_edges_do_not_force_identifier_sql(case_graph):
    session = session_for(case_graph)
    before = session.graph.accepted
    node = NodeAsset.model_validate({"id": "concept.order", "node_type": "concept", "name": "Order",
                                    "description": "An order represented by its source record identifier.",
                                    "properties": {"targets": [["column.orders.id"]]}})
    edge = GraphEdge(id="edge.order_id", edge_type="grounded_on", from_node_id=node.id,
                     to_node_id="column.orders.id", properties={"description": "Record identifier in this source."})
    trial = session.graph.save([*before.assets, node], parent_revision=before.revision, explicit_edges=[edge])
    session.graph.restore(trial.revision, before.revision)
    groups = [ChangeGroup(group_id="order", member_ids=[node.id, edge.id])]
    payload = payload_for(session, before, groups)
    units = {u["unit_id"]: u for u in payload["units"]}
    assert units[node.id]["application_requirement"] == "sql"
    assert units[edge.id]["application_requirement"] == "semantic"
    response = response_for_payload(payload)
    for unit in response["unit_results"]:
        if unit["unit_id"] == edge.id:
            unit["application"] = "not_applicable"
    AssessmentFeedback.model_validate(response).validate_evidence(payload)
    for unit in response["unit_results"]:
        if unit["unit_id"] == node.id:
            unit["application"] = "not_applicable"
    with pytest.raises(ValueError, match="SQL application is required"):
        AssessmentFeedback.model_validate(response).validate_evidence(payload)


def test_metric_physical_operand_keeps_sql_requirement():
    edge = GraphEdge(id="edge.operand", edge_type="grounded_on", from_node_id="metric.amount",
                     to_node_id="column.orders.amount", properties={"description": "Calculation input."})
    assert application_requirement({"id": edge.id}, relation=edge, owner_type="metric") == "sql"


def test_concept_assessment_does_not_require_extra_subclass_knowledge(case_graph):
    _, _, payload, _ = semantic_payload(case_graph, "concept")
    assert "subclass_value" not in payload["quality_rubric"]
    assert "subclass_value" not in payload["units"][0]["quality_requirements"]
    assert "minimality" in payload["units"][0]["quality_requirements"]
