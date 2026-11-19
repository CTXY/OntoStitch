"""Assessment evidence authority, independent dimensions and fail-closed acceptance."""
import json

import pytest
from assessment_fixtures import assessment_data, response_for_payload
from conftest import make_rule, seed_accepted
from test_agent_runtime import ScriptModel, assessment_reply, session_for

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.capabilities.sql_training.training.assessment_inputs import (
    assessment_payload,
)
from cortex.capabilities.sql_training.training.feedback import (
    AssessmentFeedback,
    assess,
)


def rule_relations(rule_id, refs):
    """Explicit fixture facts, not production inference from Rule properties."""
    return [
        {"operation": "create_edge", "edge": {
            "id": f"edge.{rule_id.removeprefix('generation_rule.')}_input_{ref.replace('.', '_')}",
            "edge_type": "grounded_on" if ref.startswith(("table.", "column.", "dimension_value.")) else "applies_to",
            "from_node_id": rule_id,
            "to_node_id": ref,
            "properties": {"description": "This input supplies the fixture rule's requested record scope."},
        }}
        for ref in refs
    ]


def write(session, name="count", refs=("table.orders",)):
    tool = next(t for t in build_tools(session) if t.name == "edit_graph")
    rule = make_rule(name)
    operations = rule_relations(rule.id, refs)
    existing = {edge.id for edge in session.graph.working.explicit_edges}
    for operation in operations:
        if operation["edge"]["id"] in existing:
            operation["operation"] = "update_edge"
    result = tool.invoke({
        "edit_id": name, "expected_revision": session.graph.working_revision,
        "upserts": [rule.model_dump(mode="json")], "edge_operations": operations,
    })
    assert result["status"] == "ok", result
    return tool


def payload_for(session, before, groups, *, validation=None, baseline=None, targets=(), prior=()):
    validation = validation if validation is not None else {"attempts":[{"attempt":1,"sql":"SELECT COUNT(*) FROM orders","correct":True,"raw_retrieved_ids":[a.id for a in session.graph.working.assets],"selected_ids":[a.id for a in session.graph.working.semantic_assets],"context_ids":[a.id for a in session.graph.working.assets],"context_edge_ids":[e.id for e in session.graph.working.graph.edges]}]}
    return assessment_payload(record=session.record,schema=session.graph.schema,baseline=baseline or before,accepted=before,trial=session.graph.working,groups=groups,targets=targets,validation=validation,prior_evidence=list(prior))


def test_group_and_coupling_constraints_only_reduce_eligibility():
    groups=[ChangeGroup(group_id="a",member_ids=["concept.a"]),ChangeGroup(group_id="b",member_ids=["metric.b"])]
    data=assessment_data(groups, ["concept.a"])
    result=AssessmentFeedback.model_validate(data)
    assert result.accepted_members(groups)==["concept.a"]
    result.coupled_unit_ids=[["concept.a","metric.b"]]
    assert result.accepted_members(groups)==[]
    result.unit_results=[]
    with pytest.raises(ValueError,match="every unit"):
        result.accepted_members(groups)


def test_scoped_units_keep_prior_definitions_and_omit_unrelated(case_graph):
    s=session_for(case_graph);write(s,"changed");write(s,"untouched",refs=())
    seed_accepted(s.graph);before=s.graph.accepted
    tool=next(t for t in build_tools(s) if t.name=="edit_graph")
    asset=make_rule("changed","new",["table.orders"])
    assert tool.invoke({"edit_id":"update","expected_revision":before.revision,"upserts":[asset.model_dump(mode="json")]})["status"]=="ok"
    s.record["gold_column_metadata"]={"internal":"not-a-prompt"}
    groups=[ChangeGroup(group_id="g",member_ids=[asset.id])]
    p=payload_for(s,before,groups,validation={"attempts":[{"attempt":1,"selected_ids":[asset.id],"raw_retrieved_ids":[asset.id],"sql":"SELECT COUNT(*) FROM orders"}]})
    assert p["round_baseline"] is None
    assert p["units"][0]["accepted_before"]["properties"]["action"]!="new"
    assert p["units"][0]["candidate"]["properties"]["action"]=="new"
    assert "generation_rule.untouched" not in json.dumps(p)
    assert "gold_column_metadata" not in p["case"]
    assert "raw_retrieved_ids" not in p["attempts"][0]
    assert p["units"][0]["context_facts"][0]["raw_retrieved"] is True


def test_actual_assessor_uses_new_schema_and_saved_relations(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id])]
    model=ScriptModel([assessment_reply])
    result=assess(model,record=s.record,schema=s.graph.schema,baseline=before,accepted=before,trial=s.graph.working,groups=groups,targets=[],validation={"attempts":[{"attempt":1,"sql":"SELECT COUNT(*) FROM orders","selected_ids":[make_rule().id],"raw_retrieved_ids":[make_rule().id]}]},context_limit=64000)
    p=json.loads(model._calls[0][1].content)
    assert result.accepted_members(groups)==[make_rule().id]
    assert any(e["type"]=="grounded_on" for e in p["related_knowledge"]["edges"])
    assert all(set(e) <= {"id", "type", "from", "to", "properties"} for e in p["related_knowledge"]["edges"])
    assert all("sources" not in e for e in p["related_knowledge"]["edges"])
    assert "Database Ontology Asset Contract" not in model._calls[0][0].content
    assert "## 1. Content and consistency" in model._calls[0][0].content


def test_assessment_format_correction_preserves_previous_defect(case_graph):
    from langchain_core.messages import AIMessage

    s = session_for(case_graph)
    before = s.graph.accepted
    write(s)
    groups = [ChangeGroup(group_id="g", member_ids=[make_rule().id])]
    invalid = assessment_data(groups, accept=[])
    invalid["unit_results"][0]["quality_checks"] = dict.fromkeys(["type_fit", "decision_value", "atomicity", "abstraction", "condition_action", "support", "graph_consistency"], "invalid")
    invalid["unit_results"][0]["feedback"][0]["location"] += ".properties.action"
    original = json.dumps(invalid)

    def correct(messages):
        assert isinstance(messages[-2], AIMessage)
        assert messages[-2].content == original
        assert "Preserve its substantive judgments" in messages[-1].content
        corrected = json.loads(messages[-2].content)
        corrected["unit_results"][0]["feedback"][0]["location"] = make_rule().id
        return AIMessage(content=json.dumps(corrected))

    model = ScriptModel([AIMessage(content=original), correct])
    result = assess(model, record=s.record, schema=s.graph.schema, baseline=before,
                    accepted=before, trial=s.graph.working, groups=groups, targets=[],
                    validation={"attempts": [{"attempt": 1, "selected_ids": [make_rule().id]}]},
                    context_limit=64000)
    assert len(model._calls) == 2
    assert result.unit_results[0].quality == "invalid"
    assert result.accepted_members(groups) == []


@pytest.mark.parametrize("selected,raw",[(False,True),(False,False),(None,None)])
def test_correct_quality_does_not_manufacture_injection(case_graph, selected, raw):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id])]
    attempt={"attempt":1,"sql":"SELECT COUNT(*) FROM orders"}
    if selected is not None: attempt["selected_ids"]=[]
    if raw is not None: attempt["raw_retrieved_ids"]=[make_rule().id] if raw else []
    payload=payload_for(s,before,groups,validation={"attempts":[attempt]})
    payload["units"][0]["retrieval_required"] = True
    result=AssessmentFeedback.model_validate(response_for_payload(payload));result.validate_evidence(payload)
    assert result.unit_results[0].quality == "valid"
    assert result.retrieval_results[0].status == "unknown"
    assert result.accepted_members(groups) == []


def test_injection_cannot_hide_one_missing_attempt(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id])]
    payload=payload_for(s,before,groups,validation={"attempts":[{"attempt":1,"selected_ids":[make_rule().id],"context_ids":[make_rule().id]},{"attempt":2,"selected_ids":[],"context_ids":[]}]})
    result=AssessmentFeedback.model_validate(response_for_payload(payload));result.validate_evidence(payload)
    assert result.retrieval_results[0].injected_attempt_ids == [1]
    assert result.retrieval_results[0].missing_attempt_ids == [2]
    assert result.accepted_members(groups) == []


def test_dependency_inclusion_is_derived_without_independent_selection(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;tool=write(s)
    v=tool.invoke({"edit_id":"value","expected_revision":s.graph.working_revision,"values":[{"column_id":"column.orders.region","value":"North"}]})
    value_id=v["value_ids"][0]
    assert tool.invoke({"edit_id":"bind","expected_revision":s.graph.working_revision,"upserts":[make_rule().model_dump(mode="json")],"edge_operations":rule_relations(make_rule().id,[value_id])})["status"]=="ok"
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id,value_id])]
    p=payload_for(s,before,groups)
    unit=next(u for u in p["units"] if u["unit_id"]==value_id)
    assert unit["context_facts"][0]["selected"] is False
    assert unit["context_facts"][0]["included_via"]==[make_rule().id]
    r=AssessmentFeedback.model_validate(response_for_payload(p));r.validate_evidence(p)
    assert set(r.accepted_members(groups))=={make_rule().id,value_id}


def test_deleted_relation_keeps_prior_edge_endpoints_and_checks_removal(case_graph):
    s=session_for(case_graph);tool=write(s)
    edge={"id":"edge.review","edge_type":"grounded_on","from_node_id":make_rule().id,"to_node_id":"column.orders.amount","properties":{"description":"The amount column supplies this fixture scope."}}
    assert tool.invoke({"edit_id":"edge","expected_revision":s.graph.working_revision,"edge_operations":[{"operation":"create_edge","edge":edge}]})["status"]=="ok"
    seed_accepted(s.graph);before=s.graph.accepted
    assert tool.invoke({"edit_id":"delete","expected_revision":before.revision,"edge_operations":[{"operation":"delete_edge","edge_id":edge["id"]}]})["status"]=="ok"
    groups=[ChangeGroup(group_id="g",member_ids=[edge["id"]])];p=payload_for(s,before,groups)
    assert p["units"][0]["change_kind"]=="delete"
    assert p["units"][0]["accepted_before"]["id"]==edge["id"]
    assert {edge["from_node_id"],edge["to_node_id"]}<={a["id"] for a in p["related_knowledge"]["nodes"]+p["physical_context"]}
    r=AssessmentFeedback.model_validate(response_for_payload(p));r.validate_evidence(p)
    assert r.accepted_members(groups)==[edge["id"]]


def test_target_resolution_and_regression_are_separate_from_quality(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id],target_ids=["t"])]
    p=payload_for(s,before,groups,targets=[{"target_id":"t"}],prior=[{"evidence_ref":"prior-1"}])
    data=response_for_payload(p);data["target_results"][0]["status"]="unresolved"
    data["target_results"][0]["feedback"]=[{"kind":"target","location":make_rule().id,"gap":"target not resolved in attempt 1","required_change":"implement the target behavior","preserve":"the valid rule"}]
    r=AssessmentFeedback.model_validate(data);r.validate_evidence(p)
    assert r.unit_results[0].quality=="valid" and r.accepted_members(groups)==[]
    data=response_for_payload(p);data["regressions"]=[{"prior_evidence_ref":"prior-1","attributable_unit_ids":[],"attribution":"inconclusive","feedback":[{"kind":"regression","location":make_rule().id,"gap":"count omitted","required_change":"restore count","preserve":"the prior counting behavior"}]}]
    r=AssessmentFeedback.model_validate(data);r.validate_evidence(p);assert r.accepted_members(groups)==[make_rule().id]
    r.regressions[0].prior_evidence_ref="invented"
    with pytest.raises(ValueError,match="unknown prior"):
        r.validate_evidence(p)


def test_distinct_round_baseline_is_not_replaced_by_latest_accepted(case_graph):
    s=session_for(case_graph);write(s);seed_accepted(s.graph);baseline=s.graph.accepted
    tool=next(t for t in build_tools(s) if t.name=="edit_graph")
    for name,action in [("improve","accepted"),("trial","tentative")]:
        assert tool.invoke({"edit_id":name,"expected_revision":s.graph.working_revision,"upserts":[make_rule(action=action).model_dump(mode="json")]})["status"]=="ok"
        if name=="improve":seed_accepted(s.graph)
    p=payload_for(s,s.graph.accepted,[ChangeGroup(group_id="g",member_ids=[make_rule().id])],baseline=baseline)
    assert p["round_baseline"]["nodes"][0]["properties"]["action"]!= "accepted"
    assert p["units"][0]["accepted_before"]["properties"]["action"]=="accepted"
    assert p["units"][0]["candidate"]["properties"]["action"]=="tentative"


def test_related_sql_error_does_not_rewrite_ontology_quality(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id])];payload=payload_for(s,before,groups)
    data=response_for_payload(payload)
    data["unit_results"][0]["feedback"]=[{"kind":"sql_error","location":make_rule().id,
        "attempt_ids":[1],"sql_excerpt":"COUNT(*)", "gap":"The requested filtered population was not constrained.",
        "required_change":"Clarify the supported population condition.","preserve":"The correct count operation."}]
    result=AssessmentFeedback.model_validate(data);result.validate_evidence(payload)
    assert result.unit_results[0].quality == "valid"
    assert not result.acceptable(groups)


def test_actual_context_presence_does_not_manufacture_dependency_provenance(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;tool=write(s,"root")
    write(s,"other",refs=())
    assert tool.invoke({"edit_id":"custom","expected_revision":s.graph.working_revision,
        "relation_contracts": [{"id": "uses_context", "name": "Uses context",
            "description": "A rule has contextual association with another rule without calculation dependency.",
            "from_node_types": ["generation_rule"], "to_node_types": ["generation_rule"],
            "direction_semantics": "Rule to its contextual rule.",
            "evidence_requirement": "An explicitly supported contextual association."}],
        "edge_operations":[{"operation":"create_edge","edge":{"id":"edge.association","edge_type":"uses_context","from_node_id":"generation_rule.root","to_node_id":"generation_rule.other","properties":{"description":"The other rule provides related context, not a declared calculation dependency."}}}]})["status"]=="ok"
    groups=[ChangeGroup(group_id="g",member_ids=["generation_rule.other"])]
    p=payload_for(s,before,groups,validation={"attempts":[{"attempt":1,"selected_ids":["generation_rule.root"],"raw_retrieved_ids":["generation_rule.root"],"context_ids":[a.id for a in s.graph.working.assets],"context_edge_ids":[e.id for e in s.graph.working.graph.edges]}]})
    assert p["units"][0]["context_facts"][0]["included_via"]==[]
    assert "generation_rule.root" in {a["id"] for a in p["related_knowledge"]["nodes"]}
    r=AssessmentFeedback.model_validate(response_for_payload(p));r.validate_evidence(p)
    assert r.accepted_members(groups)==["generation_rule.other"]


def test_old_group_verdict_protocol_is_not_accepted():
    with pytest.raises(ValueError):
        AssessmentFeedback.model_validate({"group_results":[],"target_results":[]})


def test_attributable_regression_requires_specific_feedback(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id])]
    p=payload_for(s,before,groups,prior=[{"evidence_ref":"prior-1"}])
    data=response_for_payload(p);data["regressions"]=[{"prior_evidence_ref":"prior-1","attributable_unit_ids":[make_rule().id],"attribution":"supported","feedback":[]}]
    with pytest.raises(ValueError):
        AssessmentFeedback.model_validate(data)
    data["regressions"][0]["feedback"]=[{"kind":"regression","location":make_rule().id,"gap":"the changed scope loses the prior count","required_change":"restore the prior count scope","preserve":"the previously correct counting population"}]
    r=AssessmentFeedback.model_validate(data);r.validate_evidence(p)
    assert not r.acceptable(groups)


def test_graph_dependency_does_not_prove_it_was_passed_to_generation(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;tool=write(s)
    v=tool.invoke({"edit_id":"value","expected_revision":s.graph.working_revision,"values":[{"column_id":"column.orders.region","value":"North"}]})
    value_id=v["value_ids"][0]
    assert tool.invoke({"edit_id":"bind","expected_revision":s.graph.working_revision,"upserts":[make_rule().model_dump(mode="json")],"edge_operations":rule_relations(make_rule().id,[value_id])})["status"]=="ok"
    groups=[ChangeGroup(group_id="g",member_ids=[value_id])]
    p=payload_for(s,before,groups,validation={"attempts":[{"attempt":1,"selected_ids":[make_rule().id],"context_ids":[make_rule().id],"context_edge_ids":[]}]})
    fact=p["units"][0]["context_facts"][0]
    assert fact["present_in_context"] is False and fact["included_via"]==[]
    r=AssessmentFeedback.model_validate(response_for_payload(p));r.validate_evidence(p)
    assert r.retrieval_results[0].status == "not_injected"
    assert r.retrieval_results[0].injected_attempt_ids == []


def test_prior_validation_is_reused_without_extra_queries_or_bulk_context(case_graph):
    from test_agent_graph import edit
    from test_case_workflow import EditingAgent, make_workflow
    _,services,validator=make_workflow(case_graph,EditingAgent([]))
    before=services.graph.accepted
    ref=services.validate(before)
    calls=validator.generate_calls
    compact=services.compact_validation(ref)
    assert "context_ids" in compact["attempts"][0]
    assert "context_edge_ids" in compact["attempts"][0]
    trial=edit(services.graph,case_graph[1],make_rule())
    prior=services.prior_assessment_evidence(before.revision,trial.revision)
    assert len(prior)==1 and prior[0]["evidence_ref"]==ref
    assert validator.generate_calls==calls
    for attempt in services.validation_feedback(ref)["attempts"]+prior[0]["attempts"]:
        assert not {"raw_retrieved_ids","selected_ids","context_ids","context_edge_ids"} & attempt.keys()


def test_related_sql_errors_cite_each_affected_attempt(case_graph):
    s=session_for(case_graph);before=s.graph.accepted;write(s)
    groups=[ChangeGroup(group_id="g",member_ids=[make_rule().id])]
    payload=payload_for(s,before,groups,validation={"attempts":[
        {"attempt":1,"sql":"SELECT COUNT(*) FROM orders","selected_ids":[make_rule().id]},
        {"attempt":2,"sql":"SELECT COUNT(*) FROM orders","selected_ids":[make_rule().id]},
        {"attempt":3,"sql":"SELECT SUM(amount) FROM orders","selected_ids":[make_rule().id]}]})
    data=response_for_payload(payload)
    data["unit_results"][0]["feedback"]=[
        {"kind":"sql_error","location":make_rule().id,"attempt_ids":[2],"sql_excerpt":"COUNT(*)",
         "gap":"The population filter is missing.","required_change":"Clarify the population.","preserve":"The count operation."},
        {"kind":"sql_error","location":make_rule().id,"attempt_ids":[3],"sql_excerpt":"SUM(amount)",
         "gap":"The measure changed to an amount sum.","required_change":"Restore the count behavior.","preserve":"The population."}]
    result=AssessmentFeedback.model_validate(data);result.validate_evidence(payload)
    assert result.accepted_members(groups) == []
    assert len(result.unit_results[0].feedback) == 2
    data["unit_results"][0]["feedback"][1]["attempt_ids"]=[2]
    with pytest.raises(ValueError, match="sql_excerpt"):
        AssessmentFeedback.model_validate(data).validate_evidence(payload)


def test_legacy_per_attempt_response_is_rejected():
    data = {"unit_results": [{"unit_id": "concept.a", "quality": {"status": "valid", "evidence": "ok"},
                             "application_by_attempt": [{"attempt_id": 1, "status": "applied", "evidence": "ok"}]}],
            "target_results": []}
    with pytest.raises(ValueError):
        AssessmentFeedback.model_validate(data)


def test_unresolved_target_requires_actionable_feedback(case_graph):
    s = session_for(case_graph)
    before = s.graph.accepted
    write(s)
    groups = [ChangeGroup(group_id="g", member_ids=[make_rule().id], target_ids=["t"])]
    p = payload_for(s, before, groups, targets=[{"target_id": "t"}])
    data = response_for_payload(p)
    data["target_results"][0]["status"] = "unresolved"
    with pytest.raises(ValueError, match="target feedback"):
        AssessmentFeedback.model_validate(data).validate_evidence(p)


def test_assessment_keeps_original_metadata_for_relevant_columns(case_graph):
    s = session_for(case_graph)
    before = s.graph.accepted
    write(s, "metadata", refs=("column.orders.amount",))
    trial = s.graph.working
    metadata = {"external:orders.csv": {"business_name": "Charge", "description": "Original business definition", "value_description": "Original encoding"}}
    column = next(a for a in trial.assets if a.id == "column.orders.amount")
    updated = column.model_copy(update={"properties": column.properties.model_copy(update={"source_metadata": metadata})})
    trial = trial.model_copy(update={"assets": [updated if a.id == column.id else a for a in trial.assets]})
    groups = [ChangeGroup(group_id="g", member_ids=["generation_rule.metadata"])]
    payload = assessment_payload(record=s.record, schema=s.graph.schema, baseline=before, accepted=before, trial=trial, groups=groups, targets=[], validation={"attempts": []})
    physical = {a["id"]: a for a in payload["physical_context"]}
    assert physical[column.id]["properties"]["source_metadata"] == {"external:orders.csv": {"business_name": "Charge", "description": "Original business definition"}}
    assert "column.orders.region" not in physical
