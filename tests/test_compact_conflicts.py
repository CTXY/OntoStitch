"""Compact candidate storage, graph constraints and consumers share one contract."""

import json

import pytest
from ontology_v2_fixtures import formula_edge
from pydantic import ValidationError
from test_graph_manager import apply, create, metric, node

from cortex.application.online_context import ChoicePoint, InteractionOption
from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.identity import IdentityGrouper
from cortex.capabilities.graph_merge.models import NodeRepresentation, RepresentationRef
from cortex.capabilities.graph_merge.workflow.context import GroupContext
from cortex.capabilities.online_context.output.projection import (
    asset_summary,
    project_graph,
    semantic_asset_payload,
)
from cortex.capabilities.online_context.retrieval.index import RepresentationIndex
from cortex.infrastructure.artifacts.graph_state import FileSystemGraphStateRepository
from cortex.schema import (
    ConflictAlternative,
    GraphNode,
    GraphSpace,
    GraphState,
    NodeContent,
    NodeMergeConflict,
    SourceReference,
    load_root_schema,
)


def choice(alternatives):
    return ChoicePoint(
        choice_point_id="choose",
        decision_question="Which definition applies?",
        options=tuple(
            InteractionOption(
                option_id=f"o{index}",
                statement=f"Use {alternative}",
                candidate_sources=("memory_graph",),
                graph_refs=(alternative,),
            )
            for index, alternative in enumerate(alternatives)
        ),
    )


@pytest.fixture
def compact():
    manager = GraphManager(load_root_schema())
    assert (
        apply(
            manager,
            create(node("table", "table", {"table_name": "orders"})),
            *[
                create(node(key, "column", {"table_ref": "table", "column_name": key}))
                for key in ("amount_a", "amount_b")
            ],
        ).status
        == "applied"
    )
    contents = []
    for ref in ("amount_a", "amount_b"):
        data = metric("sales", ref)
        data["description"] = "The shared business description appears once"
        data["properties"]["pattern"] = "Reusable sales definition"
        data["properties"]["sql_generation_plans"] = [
            {
                "id": "main",
                "application": "The shared calculation plan appears once",
                "steps": ["Aggregate the selected amount"],
            }
        ]
        contents.append(
            NodeContent.model_validate(
                {
                    key: data.get(key, {"aliases": [], "tags": []}.get(key))
                    for key in NodeContent.model_fields
                }
            )
        )
    owner = GraphNode.model_validate(metric("sales", "amount_a"))
    owner, conflict = manager.compact_conflict(
        owner,
        [
            (key, content, (SourceReference(source_id=key),))
            for key, content in zip(("gross", "net"), contents, strict=True)
        ],
    )
    state, _ = manager.prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=(*manager.get_snapshot().graph.nodes, owner),
                edges=(
                    *manager.get_snapshot().graph.edges,
                    formula_edge(owner.id, "amount_a", "gross"),
                    formula_edge(owner.id, "amount_b", "net"),
                ),
            ),
            node_conflicts=(conflict,),
        )
    )
    manager = GraphManager(manager.schema, state)
    return manager, contents


def test_common_fields_stored_once_and_complete_candidates_restore(compact):
    manager, contents = compact
    state = manager.get_snapshot()
    serialized = state.model_dump_json()
    assert serialized.count("The shared business description appears once") == 1
    assert serialized.count("The shared calculation plan appears once") == 1
    owner = manager.get_node("sales")
    assert "expression" not in owner.properties and "references" not in owner.properties
    for alternative, expected in zip(
        manager.get_conflict("sales").alternatives, contents, strict=True
    ):
        assert set(alternative.overrides) == {"properties.expression"}
        assert "content" not in alternative.model_dump()
        assert manager.resolve_alternative(owner, alternative) == expected
        restored = manager.get_node("sales", alternative_id=alternative.alternative_id)
        assert restored.properties == expected.properties
        assert restored.merge_status == "normal"
    assert manager.get_node("sales").properties == owner.properties


def test_optional_field_removal_uses_null_override(compact):
    manager, contents = compact
    left = contents[0].model_copy(deep=True)
    right = left.model_copy(deep=True)
    right.properties.pop("pattern")
    owner, conflict = manager.compact_conflict(
        manager.get_node("sales"), [("a", left, ()), ("b", right, ())]
    )
    assert conflict.alternatives[1].overrides == {"properties.pattern": None}
    assert manager.resolve_alternative(owner, conflict.alternatives[0]) == left
    assert manager.resolve_alternative(owner, conflict.alternatives[1]) == right


def test_shared_updates_are_validated_against_every_candidate(compact):
    manager, _ = compact
    state = manager.get_snapshot()
    common = next(n for n in state.graph.nodes if n.id == "sales")
    common.properties["pattern"] = "New shared wording"
    reader = GraphManager(manager.schema, state)
    assert (
        reader.get_node("sales", alternative_id="net").properties["pattern"]
        == "New shared wording"
    )
    common.properties["sql_generation_plans"][0]["id"] = "invalid-id"
    with pytest.raises(GraphRejected):
        manager.validate_state(state)


def test_variants_cannot_repeat_their_variable_fields_in_common(compact):
    manager, _ = compact
    state = manager.get_snapshot()
    next(n for n in state.graph.nodes if n.id == "sales").properties["expression"] = {
        "expr": "SUM(${x})",
        "refs": {"x": "amount_a"},
    }
    with pytest.raises(GraphRejected):
        manager.validate_state(state)


@pytest.mark.parametrize(
    "path",
    [
        "id",
        "sources",
        "properties",
        "properties.expression.expr",
        "properties.targets.0",
        "properties.",
    ],
)
def test_arbitrary_patch_paths_are_rejected(path):
    with pytest.raises(ValueError, match="unsupported alternative field"):
        NodeMergeConflict(
            node_id="m",
            alternatives=(
                ConflictAlternative(alternative_id="a", overrides={path: "a"}),
                ConflictAlternative(alternative_id="b", overrides={path: "b"}),
            ),
        )


def test_candidates_must_have_same_paths_and_distinct_content():
    for overrides in [
        ({}, {}),
        ({"name": "a"}, {"description": "b"}),
        ({"name": "same"}, {"name": "same"}),
    ]:
        with pytest.raises(ValueError):
            NodeMergeConflict(
                node_id="m",
                alternatives=tuple(
                    ConflictAlternative(alternative_id=key, overrides=value)
                    for key, value in zip(("a", "b"), overrides, strict=True)
                ),
            )


def test_old_full_content_and_graph_audit_fields_are_rejected(compact):
    manager, contents = compact
    with pytest.raises(ValidationError):
        ConflictAlternative(alternative_id="old", content=contents[0].model_dump())
    payload = manager.get_conflict("sales").as_conflict().model_dump(mode="json")
    payload["consolidation"] = {"proposal": "old audit"}
    with pytest.raises(ValidationError):
        NodeMergeConflict.model_validate(payload)
    payload.pop("consolidation")
    payload["consolidation_status"] = None
    with pytest.raises(ValidationError):
        NodeMergeConflict.model_validate(payload)


def test_projection_selects_restored_candidate_and_persisted_branch_edges(compact):
    manager, contents = compact
    graph, _ = project_graph(
        schema=manager.schema,
        state=manager.get_snapshot(),
        selected_node_ids={"sales"},
        choice_points=(choice(("gross", "net")),),
        selected_by_point={"choose": ("o1",)},
    )
    assert (
        next(n for n in graph.nodes if n.id == "sales").properties
        == contents[1].properties
    )
    assert {n.id for n in graph.nodes} == {"sales", "amount_b", "table"}
    assert {e.to_node_id for e in graph.edges if e.from_node_id == "sales"} == {
        "amount_b"
    }


def test_default_agent_and_retrieval_views_contain_only_differences(compact):
    manager, _ = compact
    owner = manager.get_node("sales")
    conflict = manager.get_conflict("sales")
    payload = semantic_asset_payload(
        owner, conflict, relations=manager.get_edges("sales")
    )
    assert payload["common"]["properties"] == owner.properties
    assert all(
        set(a) == {"alternative_id", "overrides", "sources"}
        for a in payload["alternatives"]
    )
    assert json.dumps(payload).count("The shared calculation plan appears once") == 1
    assert "properties.expression" in asset_summary(payload, chinese=False)
    index = RepresentationIndex.build(manager.get_snapshot())
    variants = [
        d for d in index.documents if d.representation_kind == "conflict_alternative"
    ]
    assert len(variants) == 2
    assert all(
        "content" not in d.structured_signals and "overrides" in d.structured_signals
        for d in variants
    )
    assert any("amount_b" in d.searchable_text for d in variants)


def test_repository_roundtrip_uses_new_format_without_recompiling(
    compact, tmp_path, monkeypatch
):
    manager, _ = compact
    repository = FileSystemGraphStateRepository(tmp_path, manager.schema)
    repository.save(manager.get_snapshot())
    monkeypatch.setattr(
        GraphManager,
        "prepare_state",
        lambda *args, **kwargs: pytest.fail("Read recompiled relations"),
    )
    assert repository.load() == manager.get_snapshot()


def test_merge_agent_context_shares_common_content_even_for_repeated_members():
    from cortex.capabilities.graph_merge.models import FinalNodeGroup

    contents = [
        NodeContent(
            name="Sales",
            description="Shared context marker",
            aliases=(),
            tags=(),
            properties={"expression": {"expr": f"COUNT(*) + {i}", "refs": {}}},
        )
        for i in (1, 1, 2)
    ]
    group = FinalNodeGroup(
        group_id="new",
        node_type="metric",
        existing_node_id=None,
        members=tuple(
            NodeRepresentation(
                ref=RepresentationRef(origin="incoming_node", ref_id=str(i)),
                node_type="metric",
                content=content,
                sources=(),
            )
            for i, content in enumerate(contents)
        ),
    )
    payload = GroupContext(group=group, schema_definition=load_root_schema()).payload()[
        "group"
    ]
    assert json.dumps(payload).count("Shared context marker") == 1
    assert all("content" not in member for member in payload["members"])
    assert payload["members"][0]["overrides"] == payload["members"][1]["overrides"]


def test_identity_matching_reads_all_existing_candidates(compact):
    from cortex.capabilities.graph_merge.models import NodeIdentitySelection

    manager, contents = compact
    owner = manager.get_node("sales")
    observed = []

    class Resolver:
        def resolve(self, seed, entries, queries, policy):
            observed.extend(entries[0].representations)
            return NodeIdentitySelection(
                selected_entry_id=entries[0].entry_id,
                query_verdicts=(),
                reason="Same named term",
            )

    incoming = GraphNode(
        id="incoming", node_type="metric", **contents[1].model_dump(mode="json")
    )
    conflict = manager.get_conflict("sales")
    variants = tuple(
        NodeRepresentation(
            ref=RepresentationRef(
                origin="existing_alternative", ref_id=a.alternative_id
            ),
            node_type="metric",
            content=manager.resolve_alternative(owner, a),
            sources=a.sources,
        )
        for a in conflict.alternatives
    )
    result = IdentityGrouper(resolver=Resolver()).group(
        (owner,),
        (incoming,),
        manager.schema.node_type_index(),
        existing_alternatives={"sales": variants},
    )
    assert len(observed) == 2
    assert len(result.groups) == 1
    assert {m.ref.origin for m in result.groups[0].members} == {
        "existing_node",
        "incoming_node",
    }


def test_existing_compact_conflict_can_merge_again_without_losing_candidate_ids(
    compact,
):
    from cortex.bootstrap.graph_merge import build_graph_merge_operator
    from cortex.capabilities.graph_merge.models import NodeIdentitySelection
    from cortex.config import CortexConfig

    manager, contents = compact

    class Resolver:
        def resolve(self, seed, entries, queries, policy):
            return NodeIdentitySelection(
                selected_entry_id=entries[0].entry_id,
                query_verdicts=(),
                reason="Same sales term",
            )

    incoming = GraphState(
        graph=GraphSpace(
            nodes=(
                GraphNode(
                    id="incoming_sales",
                    node_type="metric",
                    **contents[1].model_dump(mode="json"),
                    sources=(SourceReference(source_id="third"),),
                ),
            ),
            edges=(formula_edge("incoming_sales", "amount_b"),),
        ),
        node_conflicts=(),
    )
    operator = build_graph_merge_operator(
        CortexConfig(), identity_resolver=Resolver(), live_semantics=False
    )
    result = operator.merge(manager.get_snapshot(), incoming, manager.schema)
    reader = GraphManager(manager.schema, result.state)
    assert len(result.state.conflict_nodes) == 1
    assert {a.alternative_id for a in reader.get_conflict("sales").alternatives} == {
        "gross",
        "net",
    }
    assert {
        s.source_id for s in reader.get_node("sales", alternative_id="net").sources
    } == {"net", "third"}
    assert (
        reader.get_node("sales", alternative_id="net").properties
        == contents[1].properties
    )


def test_documented_schema_example_is_a_valid_complete_graph():
    from pathlib import Path

    path = Path(__file__).parents[1] / "docs/design/examples/compact-conflict.json"
    state = GraphState.model_validate_json(path.read_text())
    manager = GraphManager(load_root_schema(), state)
    assert manager.get_node("metric.sales", alternative_id="net").properties[
        "expression"
    ]["refs"] == {"amount": "column.orders.net_amount"}


def test_invalid_restored_candidate_returns_structured_feedback(compact):
    manager, _ = compact
    state = manager.get_snapshot()
    alternatives = state.conflict_nodes[0].alternatives
    alternatives[0].overrides["name"] = ""
    alternatives[1].overrides["name"] = "Sales"
    with pytest.raises(GraphRejected) as caught:
        manager.prepare_state(state)
    assert caught.value.issues[0].code == "INVALID_FIELD"
    assert "alternatives" in caught.value.issues[0].path
