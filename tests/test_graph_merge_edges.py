"""Semantic relations stay explicit; only physical ownership is materialized."""

import pytest

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)
from cortex.schema.relationships import synchronize_reference_edges


def node(node_id, node_type, properties=None, sources=()):
    return GraphNode(
        id=node_id,
        node_type=node_type,
        name=node_id,
        properties=properties or {},
        sources=sources,
    )


def edge(edge_id, source, target, kind, properties=None, sources=()):
    return GraphEdge(
        id=edge_id,
        from_node_id=source,
        to_node_id=target,
        edge_type=kind,
        properties={
            "description": "Explicitly recorded relationship.",
            **(properties or {}),
        },
        sources=sources,
    )


def sync(edges, nodes):
    return synchronize_reference_edges(
        tuple(edges), tuple(nodes), schema=load_root_schema()
    )


def test_metric_formula_does_not_generate_or_replace_authored_relations():
    source = SourceReference(source_id="case")
    metric = node(
        "metric.a",
        "metric",
        {
            "expression": {
                "expr": "${x} / ${y}",
                "refs": {"x": "metric.b", "y": "column.t.amount"},
            }
        },
        (source,),
    )
    authored = edge("authored", metric.id, "metric.b", "depends_on", sources=(source,))
    result, dropped = sync([authored], [metric, node("metric.b", "metric")])
    assert result == (authored,)
    assert not dropped
    # An absent physical operand edge is not silently fabricated by this helper.
    assert not any(e.to_node_id == "column.t.amount" for e in result)


def test_rule_keeps_explicit_targets_and_unrelated_business_relation():
    rule = node("rule", "generation_rule", {"operation_intents": ["count"]})
    authored = (
        edge("applies", rule.id, "meaning", "applies_to"),
        edge("uses", rule.id, "field", "grounded_on"),
        edge("business", "meaning", "other", "has_attribute"),
    )
    result, dropped = sync(authored, [rule, node("meaning", "concept")])
    assert {e.id for e in result} == {e.id for e in authored}
    assert not dropped
    assert {e.id: e for e in result} == {e.id: e for e in authored}


def test_concept_targets_do_not_expand_into_edges_or_rewrite_branch_sources():
    a, b = SourceReference(source_id="a"), SourceReference(source_id="b")
    concept = node("meaning", "concept", {"targets": [["field", "value"], ["other"]]})
    authored = (
        edge(
            "branch_a",
            concept.id,
            "value",
            "grounded_on",
            {"alternative_ids": ["a"]},
            (a,),
        ),
        edge(
            "branch_b",
            concept.id,
            "other",
            "grounded_on",
            {"alternative_ids": ["b"]},
            (b,),
        ),
    )
    result, dropped = sync(authored, [concept])
    assert {e.id: e for e in result} == {e.id: e for e in authored}
    assert not dropped
    assert not any(e.to_node_id == "field" for e in result)


def test_missing_formula_relation_is_rejected_by_graph_validation_not_invented():
    table = node("table.t", "table", {"table_name": "t"})
    field = node(
        "column.t.amount", "column", {"table_ref": table.id, "column_name": "amount"}
    )
    metric = node(
        "metric.total",
        "metric",
        {"expression": {"expr": "SUM(${x})", "refs": {"x": field.id}}},
    )
    with pytest.raises(GraphRejected) as caught:
        GraphManager(load_root_schema()).prepare_state(
            GraphState(graph=GraphSpace(nodes=(table, field, metric)))
        )
    assert any(item.code == "MISSING_FORMULA_RELATION" for item in caught.value.issues)


def test_unresolved_formula_reference_is_rejected_by_graph_content_validation():
    metric = node(
        "metric.total",
        "metric",
        {"expression": {"expr": "SUM(${x})", "refs": {"x": "column.t.missing"}}},
    )
    with pytest.raises(GraphRejected) as caught:
        GraphManager(load_root_schema()).prepare_state(
            GraphState(graph=GraphSpace(nodes=(metric,)))
        )
    assert any(item.code == "REFERENCE_NOT_FOUND" for item in caught.value.issues)


def test_catalog_ownership_is_the_only_generated_relation_and_is_described():
    table = node("table.t", "table", {"table_name": "t"})
    column = node(
        "column.t.status", "column", {"table_ref": table.id, "column_name": "status"}
    )
    value = node(
        "dimension_value.t.closed",
        "dimension_value",
        {"column_ref": column.id, "value": "Closed"},
    )
    result, dropped = sync([], [table, column, value])
    assert {(e.edge_type, e.from_node_id, e.to_node_id) for e in result} == {
        ("belongs_to", column.id, table.id),
        ("value_of", value.id, column.id),
    }
    assert all(e.properties["description"].strip() for e in result)
    assert not dropped
    assert sync(result, [table, column, value])[0] == result


def test_unchanged_semantic_groups_are_not_recompiled():
    existing = edge("saved", "metric.total", "column.t.amount", "grounded_on")
    result, dropped = synchronize_reference_edges(
        (existing,), (), schema=load_root_schema(), node_ids=set()
    )
    assert result == (existing,)
    assert not dropped
