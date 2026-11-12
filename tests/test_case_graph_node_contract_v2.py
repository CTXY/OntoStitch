"""Executable examples of the revised matching and calculation contract."""

import pytest
from pydantic import ValidationError

from cortex.capabilities.sql_training.contracts.semantic_mapping import (
    normalize_semantic_mapping_candidate,
    validate_semantic_mapping_candidate_contract,
)
from cortex.capabilities.sql_training.models import (
    ConceptProperties,
    EventProperties,
    GenerationRuleProperties,
    MetricProperties,
    NodeAsset,
)
from cortex.schema.references import content_reference_ids, remap_references


def concept(groups, **kwargs):
    return NodeAsset.model_validate({
        "id": "concept.school",
        "node_type": "concept",
        "name": "School",
        "description": "A school recorded in the catalog.",
        "properties": {"targets": groups, **kwargs},
    })


def test_matching_groups_preserve_joint_inputs_and_allow_shared_targets():
    groups = [["column.schools.CDSCode", "column.frpm.CDSCode"], ["table.schools"]]
    first, second = concept(groups), concept(groups)
    assert first.properties.targets == second.properties.targets == groups
    assert first.references() == [*groups[0], "table.schools"]


@pytest.mark.parametrize("groups", [[], [[]], [["concept.school"]], [["table.schools", "table.schools"]], [["table.schools"], ["table.schools"]]])
def test_invalid_matching_groups_rejected(groups):
    with pytest.raises(ValidationError):
        ConceptProperties(targets=groups)


@pytest.mark.parametrize("expressions", [{"1": "schools.CDSCode"}, {"00": "schools.CDSCode"}, {"0": ""}])
def test_complex_expressions_must_belong_to_an_existing_group(expressions):
    with pytest.raises(ValidationError):
        ConceptProperties(targets=[["column.schools.CDSCode"]], target_expressions=expressions)


def test_event_can_use_explicit_business_anchors_without_fake_sql_predicate():
    assert EventProperties().targets == []
    assert EventProperties(targets=[["table.trans"]]).targets == [["table.trans"]]


@pytest.mark.parametrize("formula", ["${x}", "CAST(${x} AS REAL)", "${x} * 1", "0 + ${x}", "(${x} / 1)"])
def test_stored_inputs_and_identity_wrappers_are_not_metrics(formula):
    with pytest.raises(ValidationError, match="must calculate"):
        MetricProperties(expression={"expr": formula, "refs": {"x": "concept.enrollment"}})


def test_metric_uses_concept_operands_and_actual_scaling():
    metric = MetricProperties(expression={"expr": "100 * ${n} / ${d}", "refs": {"n": "concept.eligible", "d": "concept.enrollment"}})
    assert metric.expression.refs["n"] == "concept.eligible"
    assert "references" not in metric.model_dump()
    assert MetricProperties(expression={"expr": "${x} / 100", "refs": {"x": "concept.amount"}})


def test_rule_accepts_multiple_existing_or_new_labels_without_generic_refs():
    rule = GenerationRuleProperties(
        operation_intents=["aggregation_grain", "new_domain_behavior"],
        triggers={"question": {"phrases": ["monthly average"]}},
        condition="An evidenced full-year monthly average is requested.",
        action="Divide the annual count by twelve.",
        reason="The case defines twelve months as the denominator.",
    )
    assert rule.operation_intents == ["aggregation_grain", "new_domain_behavior"]
    assert "references" not in rule.model_dump()


def test_matching_refs_remap_without_rewriting_sql_or_materializing_edges():
    properties = {"targets": [["column.a.id", "column.b.id"]], "target_expressions": {"0": "a.id = b.id"}}
    remapped = remap_references(properties, {"column.a.id": "column.c.id"})
    assert remapped["targets"] == [["column.c.id", "column.b.id"]]
    assert remapped["target_expressions"] == properties["target_expressions"]
    assert content_reference_ids(remapped) == {"column.c.id", "column.b.id"}


def test_complex_match_expression_is_qualified_and_schema_checked():
    schema = {"schools": "- CDSCode: TEXT\n- StatusType: TEXT"}
    asset = concept([["column.schools.StatusType"]], target_expressions={"0": "StatusType = 'Closed'"})
    normalized = normalize_semantic_mapping_candidate(asset, table_schema=schema)
    assert "schools.StatusType" in normalized.properties.target_expressions["0"]
    validate_semantic_mapping_candidate_contract(normalized, table_schema=schema)
    invalid = concept([["column.schools.StatusType"]], target_expressions={"0": "schools.DoesNotExist = 'Closed'"})
    with pytest.raises(ValueError, match="unknown column"):
        validate_semantic_mapping_candidate_contract(invalid, table_schema=schema)
