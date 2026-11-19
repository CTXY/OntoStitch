"""Quality, injection observations and SQL defects are independent dimensions."""

import pytest

from assessment_fixtures import response_for_payload

from test_agent_feedback import payload_for

from test_agent_runtime import session_for

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup

from cortex.capabilities.sql_training.models import NodeAsset

from cortex.capabilities.sql_training.training.assessment_models import (
    AssessmentFeedback,
    LearningFeedback,
)

from cortex.schema import GraphEdge

def semantic_response(payload):
    return AssessmentFeedback.model_validate(response_for_payload(payload))

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
    assert "application_requirement" not in payload["units"][0]
    response = semantic_response(payload)
    response.validate_evidence(payload)
    assert response.accepted_members(groups) == [edge.id]

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

def test_concept_assessment_does_not_require_extra_subclass_knowledge(case_graph):
    _, _, payload, _ = semantic_payload(case_graph, "concept")
    assert "subclass_value" not in payload["quality_rubric"]
    assert "subclass_value" not in payload["units"][0]["quality_requirements"]
    assert "minimality" in payload["units"][0]["quality_requirements"]

def test_supported_event_is_not_required_to_appear_in_sql(case_graph):
    _, _, payload, groups = semantic_payload(case_graph)
    result = semantic_response(payload)
    result.validate_evidence(payload)
    assert result.unit_results[0].quality == "valid"
    assert result.retrieval_results[0].status == "not_injected"
    assert not result.retrieval_results[0].required
    assert result.acceptable(groups)


def test_assessor_cannot_restore_legacy_application_protocol(case_graph):
    _, _, payload, _ = semantic_payload(case_graph)
    data = response_for_payload(payload)
    data["unit_results"][0]["application"] = "applied"
    with pytest.raises(ValueError, match="Extra inputs"):
        AssessmentFeedback.model_validate(data)
