"""Occurrence structure and explicit role facts use the shared graph contract."""

import pytest
from pydantic import ValidationError

from cortex.capabilities.sql_training.contracts.references import (
    validate_asset_batch_references,
)
from cortex.capabilities.sql_training.graph.nodes import build_graph, build_node
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import (
    GraphEdge,
    GraphSchemaError,
    GraphSpace,
    load_root_schema,
    validate_graph_space,
)
from cortex.schema.relationships import edge_identity_key, is_dependency_edge


def event(*, targets=(), **overrides):
    return NodeAsset.model_validate({
        "id": "event.purchase", "node_type": "event", "name": "Purchase",
        "description": "A customer purchases a product at the recorded purchase time; not a specific record.",
        "properties": {"targets": list(targets)}, **overrides,
    })


def concept():
    return NodeAsset.model_validate({
        "id": "concept.product", "node_type": "concept", "name": "Product",
        "description": "A product offered for purchase.",
        "properties": {"targets": [["table.products"]]},
    })


def physical():
    return NodeAsset.model_validate({
        "id": "table.products", "node_type": "table", "name": "Products",
        "properties": {"table_name": "products"},
    })


def participant(predicate="purchases", **properties):
    return GraphEdge(
        id=f"edge.{predicate}", edge_type=predicate,
        from_node_id="event.purchase", to_node_id="concept.product",
        properties={"description": "Product is the item acquired in this purchase.", **properties},
    )


def business_graph(*edges):
    return GraphSpace(
        nodes=tuple(build_node(a) for a in (physical(), event(), concept())),
        edges=edges,
    )


def test_event_requires_definition_and_allows_optional_matching_groups():
    asset = event()
    node = build_node(asset)
    assert node.node_type == "event"
    assert node.properties["definition"] == asset.description
    assert node.properties["targets"] == []
    validate_asset_batch_references(
        [asset, concept()], working_assets=[], table_schema={"products": []},
    )
    with pytest.raises(ValidationError, match="occurrence type description"):
        event(description=None)


def test_case_concept_requires_nonempty_matching_targets():
    validate_asset_batch_references(
        [concept()], working_assets=[], table_schema={"products": []},
    )
    with pytest.raises(ValidationError, match="targets"):
        NodeAsset.model_validate({
            "id": "concept.product", "node_type": "concept", "name": "Product",
            "description": "A useful description does not replace its binding.",
            "properties": {"targets": []},
        })


def test_explicit_event_role_uses_specific_predicate_and_description():
    relation = participant(description="Product is purchased; this edge does not assert one event per source row.")
    graph = business_graph(relation)
    validate_graph_space(graph, load_root_schema())
    assert graph.edges[0].properties == relation.properties
    assert not is_dependency_edge(relation)
    assert "role" not in relation.properties and "qualifier" not in relation.properties


@pytest.mark.parametrize("description", [None, "", "   "])
def test_event_role_edge_requires_nonempty_description(description):
    relation = participant().model_copy(update={
        "properties": {} if description is None else {"description": description},
    })
    with pytest.raises(GraphSchemaError, match="description"):
        validate_graph_space(business_graph(relation), load_root_schema())


@pytest.mark.parametrize("legacy_field", ["role", "condition", "qualifier", "qualifiers"])
def test_role_and_scope_are_explained_without_legacy_property_fields(legacy_field):
    relation = participant(**{legacy_field: "legacy qualification"})
    with pytest.raises(GraphSchemaError, match="description"):
        validate_graph_space(business_graph(relation), load_root_schema())


def test_specific_role_predicates_distinguish_facts_without_prose_identity():
    base = participant()
    returned = participant("returns_item")
    assert edge_identity_key(base, None) != edge_identity_key(returned, None)
    revised = participant(description="Alternative wording of the same purchased-product fact.")
    assert edge_identity_key(base, None) == edge_identity_key(revised, None)


@pytest.mark.parametrize("relation_type", ["is_a", "part_of"])
def test_classification_and_composition_do_not_cross_concept_event_roles(relation_type):
    relation = participant(relation_type)
    from cortex.schema.relation_vocabulary import legacy_relation_contracts
    graph = business_graph(relation)
    if relation_type == "part_of":
        contract = next(c for c in legacy_relation_contracts() if c.id == relation_type)
        graph = graph.model_copy(update={"relation_contracts": (contract.model_copy(
            update={"from_node_types": ["event"], "to_node_types": ["event"]}),)})
    with pytest.raises(GraphSchemaError):
        validate_graph_space(graph, load_root_schema())


def test_event_matching_groups_resolve_against_physical_catalog():
    asset = event(targets=[["column.orders.id"]])
    validate_asset_batch_references(
        [asset], working_assets=[], table_schema={"orders": "- id: INTEGER"},
    )
    with pytest.raises(ValueError, match="unresolved|grounded|unknown"):
        validate_asset_batch_references(
            [asset], working_assets=[], table_schema={"other": "- id: INTEGER"},
        )


def test_event_formal_definition_and_matching_group_validation():
    from cortex.schema.semantic_validation import validate_semantic_properties

    with pytest.raises(ValueError, match="definition"):
        validate_semantic_properties("event", {"targets": []})
    with pytest.raises(ValidationError, match="duplicate"):
        event(targets=[["column.orders.id"], ["column.orders.id"]])
    with pytest.raises(ValidationError, match="nonempty"):
        event(targets=[[]])
    with pytest.raises(ValidationError, match="group indices"):
        event(properties={"targets": [], "target_expressions": {"0": "orders.id"}})


@pytest.mark.parametrize("expression", ["orders.id", "orders.id IS NOT NULL", "orders.id + 1"])
def test_event_matching_expression_is_not_restricted_to_boolean_predicates(expression):
    asset = event(properties={
        "targets": [["column.orders.id"]], "target_expressions": {"0": expression},
    })
    validate_asset_batch_references(
        [asset], working_assets=[], table_schema={"orders": "- id: INTEGER"},
    )
    assert asset.properties.target_expressions["0"] == expression
    # Accepted syntax describes a match only; it is not proof of event semantics.


@pytest.mark.parametrize("expression", [
    "orders.id; SELECT 1", "orders.id FROM orders", "orders.id WHERE 1 = 1", "SUM(orders.id)",
])
def test_event_complex_match_rejects_query_clauses_and_aggregation(expression):
    asset = event(properties={
        "targets": [["column.orders.id"]], "target_expressions": {"0": expression},
    })
    with pytest.raises(ValueError, match="invalid|query|aggregation"):
        validate_asset_batch_references(
            [asset], working_assets=[], table_schema={"orders": "- id: INTEGER"},
        )


def test_event_formal_definition_roundtrips_through_graph_store(case_graph):
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema import GraphNode, GraphState

    store, _, _ = case_graph
    before = store.working
    formal = GraphNode(
        id="event.purchase", node_type="event", name="Purchase",
        properties={"definition": "A supported purchase occurrence type.", "targets": []},
    )
    state, _ = GraphManager(store.schema).prepare_state(GraphState(graph=GraphSpace(
        nodes=(*before.graph.nodes, formal), edges=before.graph.edges,
    )))
    saved = store.save(before.assets, parent_revision=before.revision, state=state)
    loaded = store.read(saved.revision)
    occurrence = next(a for a in loaded.semantic_assets if a.id == formal.id)
    assert occurrence.description == formal.properties["definition"]
    assert next(n for n in loaded.graph.nodes if n.id == formal.id) == build_node(occurrence)


def test_type_level_composition_cycle_requires_scope_aware_assessment():
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema.relation_vocabulary import legacy_relation_contracts

    manager = GraphManager(load_root_schema())
    first = build_node(event())
    second = first.model_copy(update={"id": "event.confirmation", "name": "Confirmation"})
    result = manager.apply([
        {"operation": "create_node", "node": n.model_dump(mode="json")}
        for n in (first, second)
    ] + [
        {"operation": "create_edge", "edge": {
            "id": f"edge.{relation}", "edge_type": relation,
            "from_node_id": first.id, "to_node_id": second.id,
            "properties": {"description": "A type-level composition statement requiring contextual semantic review."},
        }} for relation in ("part_of", "has_stage")
    ], expected_revision=manager.revision, request_id="mixed-composition-cycle",
        relation_contracts=tuple(c for c in legacy_relation_contracts() if c.id in ("part_of", "has_stage")))
    assert result.status == "applied", result.errors
    assert len(manager.get_snapshot().graph.edges) == 2
    # Structural acceptance is not semantic acceptance: Assessment must check
    # the stated business scopes rather than assume every edge is unconditional.


@pytest.mark.parametrize("relation_type", ["has_stage", "precedes"])
def test_type_level_event_order_is_not_flattened_into_a_global_dag(relation_type):
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema.relation_vocabulary import legacy_relation_contracts

    manager = GraphManager(load_root_schema())
    first = build_node(event())
    second = first.model_copy(update={"id": "event.confirmation", "name": "Confirmation"})
    result = manager.apply([
        {"operation": "create_node", "node": n.model_dump(mode="json")}
        for n in (first, second)
    ] + [
        {"operation": "create_edge", "edge": {
            "id": edge_id, "edge_type": relation_type,
            "from_node_id": source, "to_node_id": target,
            "properties": {"description": f"This ordering/composition applies only in the {edge_id} workflow variant; it is not universal."},
        }} for edge_id, source, target in (
            ("edge.forward", first.id, second.id), ("edge.backward", second.id, first.id),
        )
    ], expected_revision=manager.revision, request_id=f"cyclic-{relation_type}",
        relation_contracts=tuple(c for c in legacy_relation_contracts() if c.id == relation_type))
    assert result.status == "applied", result.errors
    assert len(manager.get_snapshot().graph.edges) == 2
    # Structural acceptance is not semantic acceptance: Assessment must check
    # the stated business scopes rather than assume every edge is unconditional.


def test_grounded_event_and_rule_scope_are_explicit_not_target_generated():
    physical_assets = [NodeAsset.model_validate({
        "id": "table.orders", "node_type": "table", "name": "Orders",
        "properties": {"table_name": "orders"},
    }), NodeAsset.model_validate({
        "id": "column.orders.id", "node_type": "column", "name": "Order ID",
        "properties": {"column_name": "id", "data_type": "INTEGER", "references": ["table.orders"]},
    })]
    occurrence = event(targets=[["column.orders.id"]])
    rule = NodeAsset.model_validate({
        "id": "generation_rule.purchase_identity", "node_type": "generation_rule", "name": "Purchase identification",
        "properties": {
            "operation_intents": ["distinct_event_identity"],
            "triggers": {"question": {"phrases": ["purchase count"]}},
            "condition": "The request counts purchases using an evidenced identity.",
            "action": "Count distinct purchase IDs.",
            "reason": "Joined detail rows can repeat an identified purchase.",
        },
    })
    base = build_graph([*physical_assets, occurrence, rule], [], load_root_schema())
    assert not any(e.from_node_id in {occurrence.id, rule.id} for e in base.edges)
    edges = [GraphEdge(
        id="edge.purchase_source", edge_type="grounded_on", from_node_id=occurrence.id,
        to_node_id="column.orders.id", properties={"description": "The documented purchase identity is recorded by orders.id."},
    ), GraphEdge(
        id="edge.rule_purchase_scope", edge_type="applies_to", from_node_id=rule.id,
        to_node_id=occurrence.id, properties={"description": "The distinct-identity decision applies to purchase counting when detail joins repeat identities."},
    )]
    graph = build_graph([*physical_assets, occurrence, rule], edges, load_root_schema())
    assert {e.id for e in graph.edges if e.from_node_id in {occurrence.id, rule.id}} == {e.id for e in edges}
