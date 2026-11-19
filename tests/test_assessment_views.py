"""Compaction preserves evidence and never weakens application attribution."""

from copy import deepcopy

import pytest
from assessment_fixtures import response_for_payload
from conftest import make_rule
from test_agent_feedback import payload_for, write
from test_agent_runtime import session_for

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.models import ColumnProperties, TableProperties
from cortex.capabilities.sql_training.training.assessment_models import (
    AssessmentFeedback,
)
from cortex.capabilities.sql_training.training.assessment_views import (
    physical_properties,
    sql_attempts,
)


def grouped_payload(case_graph):
    session = session_for(case_graph)
    before = session.graph.accepted
    write(session)
    rule = make_rule().id
    groups = [ChangeGroup(group_id="g", member_ids=[rule])]
    validation = {"attempts": [{"attempt": i, "sql": f"SELECT {i}", "correct": True, "selected_ids": [rule], "context_ids": [rule]} for i in (1, 2, 3)]}
    return payload_for(session, before, groups, validation=validation), groups


def test_identical_context_facts_are_grouped_without_discarding_sql(case_graph):
    payload, groups = grouped_payload(case_graph)
    facts = payload["units"][0]["context_facts"]
    assert len(facts) == 1 and facts[0]["attempt_ids"] == [1, 2, 3]
    assert [a["sql"] for a in payload["attempts"]] == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert "accepted_before" not in payload["units"][0]
    assert "aliases" not in payload["units"][0]["candidate"]
    result = AssessmentFeedback.model_validate(response_for_payload(payload))
    result.validate_evidence(payload)
    assert result.acceptable(groups)


@pytest.mark.parametrize("ids", [[1, 2], [1, 2, 2, 3], [1, 2, 3, 4], []])
def test_grouped_facts_reject_missing_duplicate_unknown_or_empty_attempts(case_graph, ids):
    payload, _ = grouped_payload(case_graph)
    payload["units"][0]["context_facts"][0]["attempt_ids"] = ids
    result = AssessmentFeedback.model_validate(response_for_payload(payload))
    with pytest.raises(ValueError, match="cover every validation attempt"):
        result.validate_evidence(payload)


def test_grouped_injection_reports_missing_attempt_without_a_model_verdict(case_graph):
    payload, groups = grouped_payload(case_graph)
    first=payload["units"][0]["context_facts"][0];first["attempt_ids"]=[1,2]
    payload["units"][0]["context_facts"].append({**deepcopy(first),"attempt_ids":[3],"present_in_context":False})
    result=AssessmentFeedback.model_validate(response_for_payload(payload));result.validate_evidence(payload)
    assert result.retrieval_results[0].missing_attempt_ids == [3]
    assert not result.acceptable(groups)


def test_result_views_omit_success_samples_and_keep_failure_evidence():
    sample = {"columns": ["id"], "row_count": 2, "sample_rows": [[1], [2]]}
    source = {"attempts": [
        {"attempt": 1, "sql": "a", "correct": True, "gold_result": sample, "predicted_result": sample, "internal_secret": "must not leak"},
        {"attempt": 2, "sql": "b", "correct": False, "reason": "different rows", "gold_result": sample, "predicted_result": {"row_count": 0}},
        {"attempt": 3, "sql": "c", "correct": False, "reason": "execution unavailable"},
    ]}
    original = deepcopy(source)
    a, b, c = sql_attempts(source)
    assert set(a) == {"attempt", "sql", "execution_ok", "result_equal"}
    assert a["execution_ok"] is True and a["result_equal"] is True
    assert b["difference"]["gold_result"] == sample
    assert b["difference"]["reason"] == "different rows"
    assert c["execution_ok"] is None
    assert source == original


def test_metadata_encoding_is_conditional_and_source_text_not_guessed():
    props = ColumnProperties(column_name="code", source_metadata={"external:items.csv": {"business_name": "Code", "description": "Original full meaning", "data_format": "TEXT code", "value_description": "A:first; B:second"}})
    ordinary = physical_properties(props, include_values=False)
    category = physical_properties(props, include_values=True)
    assert "value_description" not in ordinary["source_metadata"]["external:items.csv"]
    assert ordinary["source_metadata"]["external:items.csv"]["data_format"] == "TEXT code"
    assert category["source_metadata"] == props.source_metadata
    assert ordinary["source_metadata"]["external:items.csv"]["description"] == "Original full meaning"


def test_physical_compaction_preserves_declared_keys_without_expanding_catalog():
    table = TableProperties(table_name="items", unique_keys=[["column.items.id"]])
    column = ColumnProperties(column_name="id", nullable=False, key_role="PRIMARY_KEY")
    assert physical_properties(table, include_values=False)["unique_keys"] == [["column.items.id"]]
    assert physical_properties(column, include_values=False)["key_role"] == "PRIMARY_KEY"
    assert physical_properties(column, include_values=False)["nullable"] is False


def test_shared_matching_inputs_are_review_context_not_inferred_edges(case_graph):
    from test_explicit_relations import seed_pair

    store, _ = seed_pair(case_graph)
    before = store.accepted
    groups = [ChangeGroup(group_id="one_concept", member_ids=["concept.a"])]
    original = store.working.graph.model_dump(mode="json")
    payload = payload_for(session_for(case_graph), before, groups, validation={"attempts": []})
    assert {unit["unit_id"] for unit in payload["units"]} == {"concept.a"}
    assert "concept.b" in {node["id"] for node in payload["related_knowledge"]["nodes"]}
    assert "column.orders.amount" in {node["id"] for node in payload["physical_context"]}
    assert payload["related_knowledge"]["edges"] == []
    assert store.working.graph.model_dump(mode="json") == original


def test_assessment_uses_snapshot_relation_contract(case_graph):
    from test_explicit_relations import edge, seed_pair

    from cortex.capabilities.sql_training.training.assessment_inputs import (
        assessment_payload,
    )
    from cortex.schema import GraphEdge
    from cortex.schema.models import EdgeType

    store, _ = seed_pair(case_graph)
    before = store.accepted
    custom = EdgeType(
        id="classified_under", name="Classified under",
        description="A business category is classified under the target taxonomy entry.",
        from_node_types=["concept"], to_node_types=["concept"],
        direction_semantics="Specific category toward the taxonomy entry.",
        evidence_requirement="A supported classification, not a shared database field.",
    )
    relation = GraphEdge.model_validate(edge(kind=custom.id))
    trial = store.working.model_copy(update={
        "graph": store.working.graph.model_copy(update={
            "edges": (*store.working.graph.edges, relation),
            "relation_contracts": (custom,),
        }),
        "explicit_edges": [relation],
    })
    payload = assessment_payload(
        record={}, schema=store.schema, baseline=before, accepted=before,
        trial=trial, groups=[ChangeGroup(group_id="relation", member_ids=[relation.id])],
        targets=[], validation={"attempts": []},
    )
    contracts = {item["type"]: item for item in payload["related_knowledge"]["relation_definitions"]}
    assert contracts[custom.id]["description"] == custom.description
    assert contracts[custom.id]["from_node_types"] == ["concept"]
    assert contracts[custom.id]["direction_semantics"] == custom.direction_semantics
    assert contracts[custom.id]["evidence_requirement"] == custom.evidence_requirement
    assert custom.id not in store.schema.edge_type_index()


def test_unchanged_legacy_edge_gets_assessed_when_its_contract_is_added(case_graph):
    from test_explicit_relations import edge, seed_pair

    from cortex.capabilities.sql_training.graph.mutations import normalize_groups
    from cortex.capabilities.sql_training.training.assessment_inputs import (
        assessment_payload,
    )
    from cortex.schema import GraphEdge
    from cortex.schema.models import EdgeType

    store, _ = seed_pair(case_graph)
    relation = GraphEdge.model_validate(edge(kind="associated_with"))
    before = store.working.model_copy(update={
        "graph": store.working.graph.model_copy(update={"edges": (relation,)}),
        "explicit_edges": [relation],
    })
    custom = EdgeType(
        id="associated_with", name="Associated with",
        description="The source business meaning has an evidenced association with the target meaning.",
        from_node_types=["concept"], to_node_types=["concept"],
        direction_semantics="Source meaning to the associated target meaning.",
        evidence_requirement="A positive business association supported by the case.",
    )
    trial = before.model_copy(update={
        "revision": "contract-added",
        "graph": before.graph.model_copy(update={"relation_contracts": (custom,)}),
    })
    groups = normalize_groups([], before, trial)
    assert {member for group in groups for member in group.member_ids} == {relation.id}
    payload = assessment_payload(
        record={}, schema=store.schema, baseline=before, accepted=before,
        trial=trial, groups=groups, targets=[], validation={"attempts": []},
    )
    assert [unit["unit_id"] for unit in payload["units"]] == [relation.id]
    assert payload["units"][0]["candidate"] == payload["units"][0]["accepted_before"]
    assert custom.id in {item["type"] for item in payload["related_knowledge"]["relation_definitions"]}


def test_shared_new_contract_cannot_be_accepted_through_only_one_passing_legacy_edge(case_graph):
    from test_explicit_relations import concept, edge, seed_pair

    from cortex.capabilities.sql_training.graph.mutations import normalize_groups
    from cortex.capabilities.sql_training.models import NodeAsset
    from cortex.capabilities.sql_training.training.assessment_inputs import (
        assessment_payload,
    )
    from cortex.schema import GraphEdge
    from cortex.schema.models import EdgeType

    store, _ = seed_pair(case_graph)
    relations = [
        GraphEdge.model_validate(edge("first", "associated_with")),
        GraphEdge.model_validate(edge("second", "associated_with", "concept.c", "concept.d")),
    ]
    before = store.save(
        [*store.working.assets, NodeAsset.model_validate(concept("c")), NodeAsset.model_validate(concept("d"))],
        parent_revision=store.working_revision, explicit_edges=relations,
    )
    custom = EdgeType(
        id="associated_with", name="Associated with", description="An evidenced business association.",
        from_node_types=["concept"], to_node_types=["concept"],
        direction_semantics="Source business meaning toward the associated meaning.",
        evidence_requirement="A positive association supported by the case.",
    )
    trial = before.model_copy(update={
        "revision": "contract-added", "graph": before.graph.model_copy(update={"relation_contracts": (custom,)}),
    })
    proposed_groups = [ChangeGroup(group_id=str(index), member_ids=[relation.id]) for index, relation in enumerate(relations)]
    groups = normalize_groups(proposed_groups, before, trial)
    assert len(groups) == 1 and set(groups[0].member_ids) == {relation.id for relation in relations}
    payload = assessment_payload(
        record={}, schema=store.schema, baseline=before, accepted=before,
        trial=trial, groups=groups, targets=[], validation={"attempts": []},
    )
    data = response_for_payload(payload)
    for row in data["unit_results"]:
        row.update(feedback=[])
    failed = next(row for row in data["unit_results"] if row["unit_id"] == "edge.second")
    failed["quality_checks"]["predicate_precision"] = "invalid"
    failed["quality"] = "invalid"
    failed["feedback"] = [{
        "kind": "quality", "location": "edge.second", "gap": "The new contract does not describe this legacy assertion.",
        "required_change": "Reconcile the assertion before accepting the shared contract.", "preserve": "The valid meaning of edge.first.",
    }]
    result = AssessmentFeedback.model_validate(data)
    result.validate_evidence(payload)
    assert result.accepted_members(groups) == []


def test_assessment_rules_receive_one_complete_registry(case_graph):
    import json

    from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
        operation_registry,
    )
    from cortex.capabilities.sql_training.training.feedback import assessment_messages

    payload, _ = grouped_payload(case_graph)
    assert payload["operation_registry"] == operation_registry()
    assert "not a complete table catalog" in payload["physical_context_scope"]
    messages = assessment_messages(payload)
    assert "temporal_alignment" not in messages[0].content
    assert messages[1].content.count('"operation_registry":') == 1
    assert json.loads(messages[1].content)["operation_registry"] == operation_registry()


def test_prompt_view_deduplicates_only_identical_node_definition():
    from cortex.capabilities.sql_training.training.assessment_views import compact_node

    node = {"id": "concept.amount", "node_type": "concept", "description": "Recorded amount.",
            "properties": {"definition": "Recorded amount.", "targets": [["column.orders.amount"]]}}
    original = deepcopy(node)
    view = compact_node(node)
    assert "definition" not in view["properties"]
    assert view["description"] == original["properties"]["definition"]
    assert node == original
    node["properties"]["definition"] = "A different assertion requiring review."
    assert compact_node(node)["properties"]["definition"] == node["properties"]["definition"]
    ordinary = {"description": "same", "properties": {"definition": "same"}}
    assert compact_node(ordinary) == ordinary
