"""Regression coverage for graph boundaries audited together."""

import json

import pytest
from pydantic import ValidationError
from test_concept_grounding import choice
from test_explicit_relations import concept, edge, edit_tool, write
from test_graph_manager import apply, create, node

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.online_context.output.projection import project_graph
from cortex.capabilities.retrieve_db.rule_signals import generation_rule_signals
from cortex.capabilities.sql_training.graph.snapshots import fingerprint
from cortex.capabilities.sql_training.models import MetricExpression, MetricProperties
from cortex.schema import GraphEdge, GraphSpace, load_root_schema
from cortex.schema.models import EdgeType
from cortex.schema.semantic_validation import validate_semantic_properties

pytest_plugins = ["test_concept_grounding"]


@pytest.mark.parametrize(
    "legacy", ["count", ["count"], {"question": ["count"]}, {"sql": []}]
)
def test_retrieval_does_not_accept_legacy_rule_triggers(legacy):
    with pytest.raises(ValueError):
        generation_rule_signals(legacy)


def test_retrieval_reads_only_declared_nested_signals():
    assert generation_rule_signals(None) == ((), (), ())
    assert generation_rule_signals(
        {"question": {"phrases": ["count", "count"]}, "sql": {"operations": ["count"]}}
    ) == (("count",), (), ("count",))


@pytest.mark.parametrize(
    "expression",
    [{"expr": "", "refs": {}}, {"expr": "1", "refs": {"x": "column.orders.amount"}}],
)
def test_metric_expression_has_same_guard_in_model_and_formal_graph(expression):
    with pytest.raises(ValidationError):
        MetricExpression.model_validate(expression)
    with pytest.raises(ValueError):
        validate_semantic_properties(
            "metric",
            {"expression": expression, "references": list(expression["refs"].values())},
        )


@pytest.mark.parametrize(
    "plan",
    [
        {"id": "bad-id", "application": "When requested", "steps": ["Compute"]},
        {"id": "main", "application": "x" * 501, "steps": ["Compute"]},
        {"id": "main", "application": "When requested", "steps": ["x" * 601]},
    ],
)
def test_metric_plan_constraints_match_at_both_boundaries(plan):
    props = {"expression": {"expr": "1"}, "sql_generation_plans": [plan]}
    with pytest.raises(ValidationError):
        MetricProperties.model_validate(props)
    with pytest.raises(ValueError):
        validate_semantic_properties("metric", props)


def test_metric_model_rejects_mismatched_dependency_closure():
    with pytest.raises(ValidationError):
        MetricProperties.model_validate(
            {
                "expression": {"expr": "${x}", "refs": {"x": "column.orders.amount"}},
                "references": [],
            }
        )


def test_stored_marked_relation_requires_a_real_anchor(state):
    bad = GraphEdge(
        id="forged",
        edge_type="custom",
        from_node_id="concept",
        to_node_id="table",
        reference_origin="explicit",
    )
    corrupt = state.model_copy(
        update={
            "graph": GraphSpace(
                nodes=state.graph.nodes, edges=(*state.graph.edges, bad)
            )
        }
    )
    with pytest.raises(GraphRejected, match="description"):
        GraphManager(load_root_schema()).validate_state(corrupt)


def test_independent_relation_cannot_claim_an_unknown_conflict_branch(state):
    bad = GraphEdge(
        id="forged",
        edge_type="custom",
        from_node_id="concept",
        to_node_id="table",
        properties={"alternative_ids": ["unknown"]},
    )
    corrupt = state.model_copy(
        update={
            "graph": GraphSpace(
                nodes=state.graph.nodes, edges=(*state.graph.edges, bad)
            )
        }
    )
    with pytest.raises(GraphRejected):
        GraphManager(load_root_schema()).validate_state(corrupt)


def test_projection_filters_scoped_independent_relations_but_keeps_unscoped(state):
    owner = next(n for n in state.graph.nodes if n.id == "concept").model_copy(deep=True)
    owner.properties["targets"] = [["active"], ["amount"]]
    state = state.model_copy(update={"graph": GraphSpace(nodes=tuple(owner if n.id == owner.id else n for n in state.graph.nodes), edges=state.graph.edges)})
    relations = tuple(
        GraphEdge(
            id=key,
            edge_type="explained_by",
            from_node_id="concept",
            to_node_id="table",
            properties=props,
        )
        for key, props in (
            ("qualified", {"target_id": "0", "description": "Applies to the first match."}),
            ("fallback", {"target_id": "1", "description": "Applies to the second match."}),
            ("general", {"description": "Applies independently of match selection."}),
        )
    )
    state = state.model_copy(
        update={
            "graph": GraphSpace(
                nodes=state.graph.nodes, edges=(*state.graph.edges, *relations)
            )
        }
    )
    GraphManager(load_root_schema()).validate_state(state)
    projected, _ = project_graph(
        schema=load_root_schema(),
        state=state,
        selected_node_ids={"concept"},
        choice_points=(choice(("target:concept:1", "target:concept:2")),),
        selected_by_point={"choose": ("o1",)},
    )
    assert {e.id for e in projected.edges if e.edge_type == "explained_by"} == {
        "fallback",
        "general",
    }


def test_snapshot_save_keeps_override_origin_and_drops_removed_anchor(case_graph):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    custom = edge("override", "custom_measures", target="column.orders.amount")
    assert (
        write(
            tool,
            store,
            "seed",
            upserts=[concept("a")],
            relation_contracts=[{
                "id": "custom_measures", "name": "Custom measurement association",
                "description": "A business meaning is associated with a measured physical column independently of its selected match.",
                "from_node_types": ["concept"], "to_node_types": ["column"],
                "direction_semantics": "Business meaning toward the associated measurement column.",
                "evidence_requirement": "The association is independently supported, not inferred from target membership.",
            }],
            edge_operations=[{"operation": "create_edge", "edge": custom}],
        )["status"]
        == "ok"
    )
    assert store.working.explicit_edges[0].reference_origin is None
    assets = store.working.assets
    owner = next(a for a in assets if a.id == "concept.a")
    data = owner.model_dump(mode="json")
    data["properties"]["targets"] = [["column.orders.region"]]
    replacement = type(owner).model_validate(data)
    saved = store.publish_edit([replacement if a.id == owner.id else a for a in assets])
    assert [edge.id for edge in saved.explicit_edges] == ["edge.override"]
    assert {
        e.to_node_id
        for e in store.read(saved.revision).graph.edges
        if e.from_node_id == owner.id
    } == {"column.orders.amount"}


@pytest.mark.parametrize("mismatch", ["nodes", "relations"])
def test_snapshot_cannot_hide_inconsistency_behind_valid_individual_hashes(
    case_graph, mismatch
):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    assert (
        write(
            tool,
            store,
            "seed",
            upserts=[concept("a"), concept("b")],
            edge_operations=[{"operation": "create_edge", "edge": edge()}],
        )["status"]
        == "ok"
    )
    snapshot = store.working
    payload = snapshot.model_dump(mode="json")
    if mismatch == "nodes":
        next(n for n in payload["graph"]["nodes"] if n["id"] == "concept.a")["name"] = (
            "Divergent name"
        )
    else:
        next(e for e in payload["graph"]["edges"] if e["id"] == "edge.hierarchy")[
            "properties"
        ]["description"] = "Divergent relation"
    payload["graph_fingerprint"] = fingerprint(payload["graph"])
    from cortex.schema import GraphSpace

    payload["content_fingerprint"] = store._content_hash(
        snapshot.assets, snapshot.catalog_edges, snapshot.explicit_edges,
        GraphSpace.model_validate(payload["graph"]),
    )
    (store.root / f"{snapshot.revision}.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="disagree"):
        store.read(snapshot.revision)


@pytest.mark.parametrize(
    "metadata",
    [
        {"reference_origin": "default"},
        {"sources": []},
        {"properties": {"alternative_ids": ["fake"]}},
    ],
)
def test_agent_cannot_write_code_owned_relation_metadata(case_graph, metadata):
    store, _, _ = case_graph
    tool = edit_tool(case_graph)
    assert (
        write(tool, store, "seed", upserts=[concept("a"), concept("b")])["status"]
        == "ok"
    )
    before = store.working_revision
    result = write(
        tool,
        store,
        "invalid",
        edge_operations=[{"operation": "create_edge", "edge": {**edge(), **metadata}}],
    )
    assert result["status"] == "rejected"
    assert "code-managed" in str(result)
    assert store.working_revision == before


@pytest.mark.parametrize("missing", ["condition", "action", "reason", "triggers"])
def test_rule_is_complete_at_generation_boundary(missing):
    props = {
        "condition": "Count requested",
        "action": "Count rows",
        "reason": "Returns count",
        "triggers": {"question": {"phrases": ["count"]}},
    }
    props.pop(missing)
    manager = GraphManager(load_root_schema())
    assert (
        apply(manager, create(node("rule", "generation_rule", props))).status
        == "rejected"
    )


def test_explicit_override_does_not_expand_to_other_conflict_branches(state):
    from cortex.schema import (
        ConflictAlternative,
        GraphState,
        MergeStatus,
        NodeMergeConflict,
    )

    owner = next(n for n in state.graph.nodes if n.id == "concept")
    content = owner.model_dump(exclude={"id", "node_type", "sources", "merge_status"})
    content["properties"]["targets"] = [["amount"]]
    other = {**content, "description": "Another interpretation"}
    raw = GraphState(
        graph=GraphSpace(
            nodes=tuple(
                n.model_copy(
                    update={
                        "merge_status": MergeStatus.CONFLICT,
                        "properties": content["properties"],
                    }
                )
                if n.id == owner.id
                else n
                for n in state.graph.nodes
            ),
            edges=(
                GraphEdge(
                    id="branch_override",
                    edge_type="custom_measures",
                    from_node_id="concept",
                    to_node_id="amount",
                    properties={"target_id": "0", "alternative_ids": ["a"], "description": "A specific fact for the first candidate."},
                ),
            ),
        ),
        node_conflicts=(
            NodeMergeConflict(
                node_id="concept",
                alternatives=(
                    ConflictAlternative(
                        alternative_id="a",
                        overrides={"description": None},
                        sources=owner.sources,
                    ),
                    ConflictAlternative(
                        alternative_id="b",
                        overrides={"description": other["description"]},
                        sources=owner.sources,
                    ),
                ),
            ),
        ),
    )
    manager = GraphManager(load_root_schema())
    prepared, _ = manager.prepare_state(raw)
    assert {
        (e.edge_type, tuple(e.properties["alternative_ids"]))
        for e in prepared.graph.edges
        if e.from_node_id == "concept"
    } == {("custom_measures", ("a",))}
    manager.validate_state(prepared)
    for option, expected in [("o0", ["custom_measures"]), ("o1", [])]:
        projected, _ = project_graph(
            schema=manager.schema,
            state=prepared,
            selected_node_ids={"concept"},
            choice_points=(choice(("a", "b")),),
            selected_by_point={"choose": (option,)},
        )
        assert [
            e.edge_type for e in projected.edges if e.from_node_id == "concept"
        ] == expected


def test_custom_ownership_override_is_supported_by_physical_consumers(state):
    from cortex.capabilities.retrieve_db.graph_index import GraphIndex
    from cortex.capabilities.sql_training.graph.physical import (
        validate_existing_physical_graph,
    )
    from cortex.capabilities.sql_training.merge_evaluation import (
        _audit_final_physical_graph,
    )

    manager = GraphManager(load_root_schema(), state)
    result = apply(
        manager,
        *[
            {
                "operation": "create_edge",
                "edge": {
                    "id": f"custom_{source}",
                    "edge_type": "owned_by",
                    "from_node_id": source,
                    "to_node_id": target,
                },
            }
            for source, target in [("amount", "table"), ("active", "status")]
        ],
        relation_contracts=(EdgeType(
            id="owned_by", name="Physical ownership alias",
            description="A physical Column or DimensionValue points to its declared catalog owner; Column ownership is a Table and Value ownership is a Column.",
            from_node_types=["column", "dimension_value"], to_node_types=["table", "column"],
            direction_semantics="Physical child toward its declared owner.",
            evidence_requirement="The target must match the node's declared table_ref or column_ref.",
        ),),
    )
    assert result.status == "applied", result.errors
    graph = manager.get_snapshot().graph
    validate_existing_physical_graph(graph)
    audit = _audit_final_physical_graph(graph, {})
    assert not audit["invalid_value_owners"]
    index = GraphIndex.build(graph)
    assert index.columns_by_table_and_name[("table", "amount")] == ("amount",)


def test_physical_owner_relation_cannot_disagree_with_the_declared_owner(state):
    original = next(e for e in state.graph.edges if e.edge_type == "belongs_to")
    fake = original.model_copy(update={"to_node_id": "concept"})
    corrupt = state.model_copy(update={"graph": GraphSpace(nodes=state.graph.nodes, edges=tuple(fake if e.id == original.id else e for e in state.graph.edges))})
    with pytest.raises(GraphRejected):
        GraphManager(load_root_schema()).validate_state(corrupt)


def test_training_rejects_unvalidated_repository_state_before_calling_engine(state):
    from cortex.application.sql_training import QASqlTrainingRecord, SqlTrainingService

    class Repository:
        def load(self):
            # A persisted matching input is unresolved, despite valid node shapes.
            owner = next(n for n in state.graph.nodes if n.id == "concept").model_copy(deep=True)
            owner.properties["targets"] = [["missing"]]
            return state.model_copy(update={"graph": GraphSpace(nodes=tuple(owner if n.id == owner.id else n for n in state.graph.nodes), edges=state.graph.edges)})

    class NeverCalled:
        def train(self, *args):
            pytest.fail("Training engine received an invalid persisted graph")

        def merge(self, *args):
            pytest.fail("Merge received an invalid persisted graph")

    service = SqlTrainingService(
        NeverCalled(), graph_merge=NeverCalled(), state_repository=Repository()
    )
    with pytest.raises(GraphRejected):
        service.run(
            (QASqlTrainingRecord(source_id="qa", question="Count", sql="SELECT 1"),),
            load_root_schema(),
        )
