"""The bundled root is the current NL2SQL contract, without legacy conversion."""

import pytest
from pydantic import ValidationError
from test_graph_manager import apply, create, node

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.models import DifferencePosition
from cortex.capabilities.sql_training.models import (
    GenerationRuleProperties,
    MetricProperties,
)
from cortex.schema import (
    ConflictChoice,
    ConflictPoint,
    GraphNode,
    GraphSpace,
    GraphState,
    load_root_schema,
)


def metric(node_id, reference):
    reference = "column.orders.amount" if reference == "field" else reference
    return node(
        node_id,
        "metric",
        {"expression": {"expr": "SUM(${x})", "refs": {"x": reference}}},
    )


NODE_TYPES = {
    "table",
    "column",
    "dimension_value",
    "concept",
    "event",
    "metric",
    "generation_rule",
}
RELATION_TYPES = {
    "belongs_to", "value_of", "joins_with", "depends_on", "grounded_on", "applies_to", "is_a", "has_attribute",
}


def test_root_contains_only_current_nl2sql_types_and_relation_endpoints():
    schema = load_root_schema()
    assert set(schema.node_type_index()) == NODE_TYPES
    assert set(schema.edge_type_index()) == RELATION_TYPES
    assert all(
        set(e.from_node_types) <= NODE_TYPES and set(e.to_node_types) <= NODE_TYPES
        for e in schema.edge_types
    )
    assert schema.node_type_index()["metric"].identity_policy.fields == (
        "name",
        "aliases",
    )
    assert schema.effective_node_fields("metric")["expression"].type == "object"
    assert set(schema.effective_node_fields("metric")) == set(
        MetricProperties.model_fields
    )
    assert set(schema.effective_node_fields("generation_rule")) == set(
        GenerationRuleProperties.model_fields
    )
    assert "related_to" not in schema.edge_type_index()


@pytest.fixture
def manager():
    manager = GraphManager(load_root_schema())
    assert (
        apply(
            manager,
            create(node("table.orders", "table", {"table_name": "orders"})),
            create(
                node(
                    "column.orders.amount",
                    "column",
                    {"table_ref": "table.orders", "column_name": "amount"},
                )
            ),
        ).status
        == "applied"
    )
    return manager


@pytest.mark.parametrize(
    "field,value",
    [
        ("physical_dependencies", []),
        ("physical_dependencies", None),
        ("physical_dependencies", ["field"]),
        ("filters", []),
        ("allowed_dimension_refs", []),
        ("default_time", "field"),
        ("metric_name", "Old name"),
    ],
)
def test_legacy_metric_fields_are_preserved_by_raw_parsing_then_rejected(
    manager, field, value
):
    candidate = metric("m", "field")
    candidate["properties"][field] = value
    parsed = GraphNode.model_validate(candidate)
    assert field in parsed.properties and parsed.properties[field] == value
    with pytest.raises(ValidationError):
        MetricProperties.model_validate(candidate["properties"])
    result = apply(manager, create(candidate))
    assert result.status == "rejected"
    assert result.errors[0].code == "INVALID_FIELD"
    with pytest.raises(GraphRejected):
        manager.prepare_state(
            GraphState(
                graph=GraphSpace(
                    nodes=(*manager.get_snapshot().graph.nodes, parsed),
                    edges=manager.get_snapshot().graph.edges,
                ),
                node_conflicts=(),
            )
        )
    assert manager.get_node("m") is None


def test_legacy_string_metric_expression_is_rejected(manager):
    candidate = metric("m", "field")
    candidate["properties"]["expression"] = "SUM(amount)"
    assert apply(manager, create(candidate)).errors[0].code == "INVALID_FIELD"


@pytest.mark.parametrize(
    "kind",
    ["example", "knowledge_unit", "entity", "process", "rule", "join_path"],
)
def test_non_nl2sql_nodes_are_rejected_at_generation_and_crud(manager, kind):
    candidate = GraphNode(id="old", node_type=kind, name="Old type")
    assert (
        apply(manager, create(candidate.model_dump(mode="json"))).errors[0].code
        == "UNKNOWN_NODE_TYPE"
    )
    with pytest.raises(GraphRejected):
        manager.prepare_state(
            GraphState(
                graph=GraphSpace(
                    nodes=(*manager.get_snapshot().graph.nodes, candidate),
                    edges=manager.get_snapshot().graph.edges,
                ),
                node_conflicts=(),
            )
        )


def test_rule_legacy_relations_are_not_converted_to_refs():
    candidate = {
        "triggers": {"question": {"phrases": ["count"]}},
        "condition": "Count requested",
        "action": "Count rows",
        "reason": "Returns row count",
        "relations": [{"relation": "grounded_on", "node_id": "field"}],
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        GenerationRuleProperties.model_validate(candidate)


def test_conflict_legacy_fields_are_rejected_instead_of_discarded_or_renamed():
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConflictChoice.model_validate(
            {"alternative_ids": ["a"], "statement": "First", "field_paths": ["name"]}
        )
    with pytest.raises(ValidationError):
        ConflictPoint.model_validate(
            {
                "point_id": "p",
                "aspect": "meaning",
                "impact": "Old description",
                "choices": [
                    {"alternative_ids": ["a"], "statement": "A"},
                    {"alternative_ids": ["b"], "statement": "B"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        DifferencePosition.model_validate(
            {
                "members": [
                    {
                        "representation_ref": {
                            "origin": "incoming_node",
                            "ref_id": "a",
                        },
                        "source_case_ids": [],
                    }
                ],
                "statement": "A",
                "field_paths": ["name"],
            }
        )


def test_custom_relations_still_work_without_root_registration(manager):
    from cortex.schema.models import EdgeType

    result = apply(
        manager,
        create(metric("m", "field")),
        {
            "operation": "create_edge",
            "edge": {
                "id": "metric-input",
                "edge_type": "grounded_on",
                "from_node_id": "m",
                "to_node_id": "column.orders.amount",
                "properties": {
                    "description": "This amount field supplies the sum operand."
                },
            },
        },
        {
            "operation": "create_edge",
            "edge": {
                "id": "custom",
                "edge_type": "custom_measures",
                "from_node_id": "m",
                "to_node_id": "column.orders.amount",
                "properties": {
                    "description": "This metric directly uses the specified amount field."
                },
            },
        },
        relation_contracts=(EdgeType(
            id="custom_measures", name="Measurement association",
            description="A Metric has an independently described measurement association with a physical column.",
            from_node_types=["metric"], to_node_types=["column"],
            direction_semantics="Metric toward the measurement column.",
            evidence_requirement="A supported measurement association; it does not replace direct formula inputs.",
        ),),
    )
    assert result.status == "applied", result.errors
    assert ("custom_measures", None) in [
        (e.edge_type, e.reference_origin) for e in manager.get_edges("m")
    ]


def test_explicit_null_plans_are_not_silently_normalized_to_empty():
    candidate = {
        "expression": {"expr": "COUNT(*)", "refs": {}},
        "sql_generation_plans": None,
    }
    with pytest.raises(ValidationError):
        MetricProperties.model_validate(candidate)
    candidate.pop("sql_generation_plans")
    assert MetricProperties.model_validate(candidate).sql_generation_plans == []


def test_category_of_is_semantic_and_rejects_physical_endpoints(manager):
    assert apply(manager, create(node("concept.category", "concept", {"targets": [["column.orders.amount"]]})), create(node("concept.dimension", "concept", {"targets": [["column.orders.amount"]]}))).status == "applied"
    edge = {"id": "edge.category", "edge_type": "category_of", "from_node_id": "concept.category", "to_node_id": "concept.dimension", "properties": {"description": "The category belongs to this semantic dimension."}}
    from cortex.schema.relation_vocabulary import legacy_relation_contracts
    contract = next(c for c in legacy_relation_contracts() if c.id == "category_of")
    assert apply(manager, {"operation": "create_edge", "edge": edge}, relation_contracts=(contract,)).status == "applied"
    revision = manager.revision
    invalid = dict(edge, id="edge.invalid_category", from_node_id="column.orders.amount")
    assert apply(manager, {"operation": "create_edge", "edge": invalid}).status == "rejected"
    assert manager.revision == revision
