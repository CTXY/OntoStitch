"""Metric content has one plan list and no independent policy/classification fields."""

import pytest
from pydantic import ValidationError

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.online_context.output.projection import (
    asset_summary,
    semantic_asset_payload,
)
from cortex.capabilities.sql_training.graph.nodes import build_node
from cortex.capabilities.sql_training.learning.inputs import ontology_contract
from cortex.capabilities.sql_training.models import (
    MetricProperties,
    NodeAsset,
    SQLGenerationPlan,
)
from cortex.schema import GraphNode, GraphSpace, GraphState, load_root_schema


def properties():
    return {
        "pattern": "Total order amount",
        "expression": {
            "expr": "SUM(${amount})",
            "refs": {"amount": "column.orders.amount"},
        },
        "sql_generation_plans": [
            {
                "id": "orders_total",
                "application": "Orders source",
                "steps": ["Sum <orders.amount> over the selected input records."],
            }
        ],
    }


def test_plan_type_is_the_item_model_of_one_stored_field():
    metric = MetricProperties.model_validate(properties())
    assert set(MetricProperties.model_fields) == {
        "pattern",
        "expression",
        "sql_generation_plans",
    }
    assert isinstance(metric.sql_generation_plans[0], SQLGenerationPlan)
    assert set(metric.sql_generation_plans[0].model_dump()) == {
        "id",
        "application",
        "steps",
    }
    assert MetricProperties.model_validate(metric.model_dump()) == metric
    field = MetricProperties.model_json_schema()["properties"]["sql_generation_plans"]
    assert field["type"] == "array"
    assert field["items"] == {"$ref": "#/$defs/SQLGenerationPlan"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("aggregation_policy", "DIRECT_AGGREGATE"),
        ("calculation_policy", {"zero_division": "NULL"}),
        ("metric_kind", "amount"),
    ],
)
def test_removed_fields_are_rejected_by_models_and_graph_manager(field, value):
    candidate = {**properties(), field: value}
    with pytest.raises(ValidationError):
        MetricProperties.model_validate(candidate)
    schema = load_root_schema()
    assert field not in schema.effective_node_fields("metric")
    assert (
        f"properties.{field}"
        not in schema.node_type_index()["metric"].identity_policy.fields
    )
    state, _ = GraphManager(schema).prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=(
                    GraphNode(
                        id="table.orders",
                        node_type="table",
                        name="orders",
                        properties={"table_name": "orders"},
                    ),
                    GraphNode(
                        id="column.orders.amount",
                        node_type="column",
                        name="amount",
                        properties={
                            "table_ref": "table.orders",
                            "column_name": "amount",
                            "data_type": "REAL",
                        },
                    ),
                )
            ),
            node_conflicts=(),
        )
    )
    manager = GraphManager(schema, state)
    result = manager.apply(
        [
            {
                "operation": "create_node",
                "node": {
                    "id": "metric.amount",
                    "node_type": "metric",
                    "name": "amount",
                    "properties": candidate,
                },
            }
        ],
        expected_revision=manager.revision,
        request_id="old-field",
    )
    assert result.status == "rejected" and result.errors[0].code == "INVALID_FIELD"
    assert result.errors[0].operation_index == 0
    assert manager.get_snapshot() == state


def test_generation_online_output_and_agent_schema_share_one_plan_field():
    asset = NodeAsset.model_validate(
        {
            "id": "metric.total_amount",
            "node_type": "metric",
            "name": "Total amount",
            "properties": properties(),
        }
    )
    graph_node = build_node(asset)
    payload = semantic_asset_payload(graph_node)
    assert (
        payload["sql_generation_plans"] == graph_node.properties["sql_generation_plans"]
    )
    assert "generation_plan" not in payload and "SQLGenerationPlan" not in payload
    assert "Sum <orders.amount>" in asset_summary(payload, chinese=False)
    injected = ontology_contract(load_root_schema())["output_schema"][
        "properties_by_type"
    ]["metric"]
    assert set(injected["properties"]) == set(MetricProperties.model_fields)
    assert (
        not {"aggregation_policy", "calculation_policy", "metric_kind"} & payload.keys()
    )
