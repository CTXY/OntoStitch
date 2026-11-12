import pytest
from conftest import make_rule, seed_accepted

from cortex.capabilities.sql_training.graph.mutations import (
    ChangeGroup,
    GraphEdit,
    apply_edit,
    normalize_groups,
)
from cortex.capabilities.sql_training.models import NodeAsset


def metric_edges(asset):
    if asset.node_type.value != "metric":
        return []
    return [
        {"operation": "create_edge", "edge": {
            "id": f"edge.{asset.id}.{key}",
            "edge_type": "depends_on" if ref.startswith(("concept.", "metric.")) else "grounded_on",
            "from_node_id": asset.id,
            "to_node_id": ref,
            "properties": {"description": f"Operand {key} supplies the calculation input."},
        }}
        for key, ref in asset.properties.expression.refs.items()
    ]


def edit(store, database, asset, operation="create", edit_id="e1"):
    edges = metric_edges(asset)
    existing_edges = {edge.id for edge in store.working.graph.edges}
    for item in edges:
        if item["edge"]["id"] in existing_edges:
            item["operation"] = "update_edge"
    return apply_edit(
        store,
        GraphEdit.model_validate(
            {
                "edit_id": edit_id,
                "expected_revision": store.working_revision,
                "operations": [
                    {
                        "operation": operation,
                        "node_id": asset.id,
                        "asset": asset.model_dump(mode="json"),
                    }
                ],
                "edge_operations": edges,
            }
        ),
        database=database,
        source_id="case",
    )


def test_complete_physical_graph_and_failed_edit_are_immutable(case_graph):
    store, database, _ = case_graph
    old = store.working
    assert {"table.unused", "column.unused.code"} <= {a.id for a in old.assets}
    with pytest.raises(ValueError):
        edit(store, database, metric("missing", "column.missing.nope"))
    assert store.working_revision == old.revision
    assert store.read(old.revision) == old


def test_cannot_edit_physical_or_delete_retained_nodes(case_graph):
    store, database, _ = case_graph
    table = next(a for a in store.working.assets if a.id == "table.orders")
    with pytest.raises(ValueError, match="physical"):
        edit(store, database, table, "update")
    edit(store, database, make_rule())
    seed_accepted(store)
    with pytest.raises(ValueError):
        GraphEdit.model_validate(
            {
                "edit_id": "bad",
                "expected_revision": store.working_revision,
                "operations": [{"operation": "delete", "node_id": make_rule().id}],
            }
        )


def test_m2_accepted_m3_rejected_retains_m2(case_graph):
    store, database, _ = case_graph
    edit(store, database, make_rule(action="m1"))
    seed_accepted(store)
    baseline = store.accepted_revision
    m2 = edit(store, database, make_rule(action="m2"), "update", "e2")
    selected = store.select(
        assessed_revision=m2.revision,
        accepted_before=baseline,
        member_ids=[make_rule().id],
    )
    seed_accepted(store, selected)
    accepted = store.accepted_revision
    edit(store, database, make_rule(action="m3"), "update", "e3")
    retained = store.retain()
    assert retained.semantic_assets[0].properties.action == "m2"
    assert retained.revision == accepted
    assert retained.revision != baseline


def test_acceptance_uses_complete_assessed_content_not_last_edit(case_graph):
    store, database, _ = case_graph
    edit(store, database, make_rule(action="m1"))
    seed_accepted(store)
    accepted = store.accepted_revision
    edit(store, database, make_rule(action="m2"), "update", "e2")
    current = store.working.semantic_assets[0].model_copy(update={"aliases": ["alias"]})
    snap = edit(store, database, current, "update", "e3")
    promoted = store.select(
        assessed_revision=snap.revision,
        accepted_before=accepted,
        member_ids=[current.id],
    )
    assert promoted.semantic_assets[0].properties.action == "m2"
    assert promoted.semantic_assets[0].aliases == ["alias"]
    assert store.accepted_revision == accepted
    seed_accepted(store, promoted)
    with pytest.raises(ValueError, match="stale"):
        store.select(
            assessed_revision=snap.revision,
            accepted_before=accepted,
            member_ids=[current.id],
        )


def test_value_materialization_old_revision_and_retention(case_graph):
    store, database, _ = case_graph
    old = store.working
    concept = NodeAsset.model_validate(
        {
            "id": "concept.north",
            "node_type": "concept",
            "name": "North",
            "properties": {
                "targets": [["column.orders.region"]],
                "target_expressions": {"0": "orders.region = 'North'"}
            },
        }
    )
    snap = edit(store, database, concept)
    assert any(a.id.startswith("dimension_value.") for a in snap.assets)
    assert store.read(old.revision) == old
    retained = store.retain()
    assert not retained.semantic_assets
    assert not any(a.id.startswith("dimension_value.") for a in retained.assets)


def test_edit_dedup_does_not_restore_old_head(case_graph):
    store, database, _ = case_graph
    initial = store.working_revision
    request = {
        "edit_id": "same",
        "expected_revision": initial,
        "upserts": [make_rule().model_dump(mode="json")],
    }
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    first = tool.invoke(request)
    last = edit(store, database, make_rule(action="new"), "update", "next")
    assert tool.invoke(request)["applied_revision"] == first["applied_revision"]
    assert store.working_revision == last.revision
    changed = {**request, "expected_revision": last.revision}
    rejected = tool.invoke(changed)
    assert rejected["status"] == "rejected"
    assert "different" in rejected["errors"][0]["message"]
    assert not (store.root.parent / "edits").exists()


def test_same_target_groups_merge(case_graph):
    store, database, _ = case_graph
    before = store.accepted
    edit(store, database, make_rule("a"))
    after = edit(store, database, make_rule("b"), edit_id="e2")
    groups = normalize_groups(
        [
            ChangeGroup(
                group_id="a", member_ids=["generation_rule.a"], target_ids=["t"]
            ),
            ChangeGroup(
                group_id="b", member_ids=["generation_rule.b"], target_ids=["t"]
            ),
        ],
        before,
        after,
    )
    assert len(groups) == 1
    assert len(groups[0].member_ids) == 2


def test_discarded_edit_receipt_cannot_claim_success_after_recovery(case_graph):
    store, _database, _ = case_graph
    base = store.working_revision
    request = {
        "edit_id": "lost",
        "expected_revision": base,
        "upserts": [make_rule().model_dump(mode="json")],
    }
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    tool.invoke(request)
    store.restore(base, base)
    rejected = tool.invoke(request)
    assert rejected["status"] == "rejected"
    assert "discarded" in rejected["errors"][0]["message"]
    assert store.working_revision == base
    fresh_tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    fresh_tool.invoke(request)
    assert store.working.semantic_assets[0].id == make_rule().id


def test_repeated_value_materialization_preserves_aliases_and_sources(case_graph):
    from cortex.capabilities.sql_training.core.enums import SourceType
    from cortex.capabilities.sql_training.models import Source

    store, database, _ = case_graph
    concept = NodeAsset.model_validate(
        {
            "id": "concept.north",
            "node_type": "concept",
            "name": "North",
            "properties": {
                "targets": [["column.orders.region"]],
                "target_expressions": {"0": "orders.region = 'North'"}
            },
        }
    )
    edit(store, database, concept)
    enriched = []
    for a in store.working.assets:
        if a.id.startswith("dimension_value."):
            a = a.model_copy(
                update={
                    "aliases": ["Northern"],
                    "sources": [
                        *a.sources,
                        Source(source_type=SourceType.QA, source_id="prior-case"),
                    ],
                }
            )
        enriched.append(a)
    store.publish_edit(enriched)
    snap = edit(store, database, concept, "update", "again")
    value = next(a for a in snap.assets if a.id.startswith("dimension_value."))
    assert value.aliases == ["Northern"]
    assert "prior-case" in {s.source_id for s in value.sources}


def test_empty_acceptance_does_not_advance_l(case_graph):
    store, _, _ = case_graph
    before = store.accepted_revision
    store.select(
        assessed_revision=store.working_revision, accepted_before=before, member_ids=[]
    )
    assert store.accepted_revision == before


def test_snapshot_storage_failure_does_not_apply_edit(case_graph, monkeypatch):
    import cortex.capabilities.sql_training.graph.snapshots as module

    store, database, _ = case_graph
    initial = store.working_revision
    monkeypatch.setattr(
        module,
        "write_json_atomic",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        edit(store, database, make_rule())
    assert store.working_revision == initial


def metric(name, reference):
    return NodeAsset.model_validate(
        {
            "id": f"metric.{name}",
            "node_type": "metric",
            "name": name,
            "properties": {
                "pattern": "Sum the amount.",
                "expression": {"expr": "SUM(${amount})", "refs": {"amount": reference}},
                "sql_generation_plans": [
                    {
                        "id": "main",
                        "application": "Calculate order amount.",
                        "steps": ["Sum <orders.amount> from <orders>."],
                    }
                ],
            },
        }
    )


def test_joint_metric_creation_and_dependency_group_closure(case_graph):
    store, database, _ = case_graph
    before = store.accepted
    a, b = metric("a", "column.orders.amount"), metric("b", "metric.a")
    with pytest.raises(ValueError):
        edit(store, database, b)
    request = GraphEdit.model_validate(
        {
            "edit_id": "joint",
            "expected_revision": store.working_revision,
            "operations": [
                {
                    "operation": "create",
                    "node_id": row.id,
                    "asset": row.model_dump(mode="json"),
                }
                for row in [a, b]
            ],
            "edge_operations": [*metric_edges(a), *metric_edges(b)],
        }
    )
    snap = apply_edit(store, request, database=database, source_id="case")
    groups = normalize_groups(
        [
            ChangeGroup(group_id="a", member_ids=[a.id]),
            ChangeGroup(group_id="b", member_ids=[b.id]),
        ],
        before,
        snap,
    )
    assert len(groups) == 1
    with pytest.raises(ValueError, match="depends"):
        store.select(
            assessed_revision=snap.revision,
            accepted_before=before.revision,
            member_ids=[b.id],
        )
    previous = store.working_revision
    cyclic = metric("a", "metric.b")
    with pytest.raises(ValueError, match="cycle"):
        edit(store, database, cyclic, "update", "cycle")
    assert store.working_revision == previous


def test_unmaterialized_value_is_rejected_even_under_real_column(case_graph):
    store, database, _ = case_graph
    with pytest.raises(ValueError, match="unresolved|unverified|not found"):
        edit(
            store, database,
            NodeAsset.model_validate({
                "id": "concept.invented", "node_type": "concept", "name": "Invented region",
                "properties": {"targets": [["dimension_value.orders.region.Invented"]]},
            }),
        )


def test_flat_tool_upserts_reverts_and_rejects_invalid_batch_atomically(case_graph):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    store, _, _ = case_graph
    tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    tool.invoke(
        {
            "edit_id": "create-pair",
            "expected_revision": store.working_revision,
            "upserts": [
                make_rule("a", "a1").model_dump(mode="json"),
                make_rule("b", "b1").model_dump(mode="json"),
            ],
        }
    )
    tool.invoke(
        {
            "edit_id": "update-and-revert",
            "expected_revision": store.working_revision,
            "upserts": [make_rule("a", "a2").model_dump(mode="json")],
            "revert_ids": ["generation_rule.b"],
        }
    )
    assert [(a.id, a.properties.action) for a in store.working.semantic_assets] == [
        ("generation_rule.a", "a2")
    ]
    before = store.working_revision
    rejected = tool.invoke(
        {
            "edit_id": "bad-batch",
            "expected_revision": before,
            "upserts": [
                make_rule("c").model_dump(mode="json"),
                store.working.physical_assets[0].model_dump(mode="json"),
            ],
        }
    )
    assert rejected["status"] == "rejected"
    assert rejected["errors"][0]["code"] == "OPERATION_NOT_ALLOWED"
    assert store.working_revision == before
    seed_accepted(store)
    tool.invoke(
        {
            "edit_id": "tentative",
            "expected_revision": store.working_revision,
            "upserts": [make_rule("a", "a3").model_dump(mode="json")],
        }
    )
    tool.invoke(
        {
            "edit_id": "restore-accepted",
            "expected_revision": store.working_revision,
            "revert_ids": ["generation_rule.a"],
        }
    )
    assert store.working.semantic_assets[0].properties.action == "a2"


@pytest.mark.parametrize(
    "legacy_field", ["edit", "operations", "node_id", "asset", "revert_id"]
)
def test_flat_edit_rejects_mixed_legacy_arguments_before_any_publication(
    case_graph, legacy_field
):
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    store, _, _ = case_graph
    session = session_for(case_graph)
    tool = next(t for t in build_tools(session) if t.name == "edit_graph")
    before = store.working_revision, store.accepted_revision
    files = set(store.root.glob("*.json"))
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        tool.invoke(
            {
                "edit_id": "mixed",
                "expected_revision": store.working_revision,
                "upserts": [make_rule().model_dump(mode="json")],
                legacy_field: {"must_not_be_ignored": True},
            }
        )
    assert (store.working_revision, store.accepted_revision) == before
    assert set(store.root.glob("*.json")) == files
    assert not session.edit_receipts and not session.groups
    assert not store.working.semantic_assets


@pytest.mark.parametrize("expr", ["SUM(amount)", "SUM({amount})", "SUM(<amount>)"])
def test_metric_placeholder_error_names_exact_syntax_and_publishes_nothing(
    case_graph, expr
):
    store, _, _ = case_graph
    before = store.working_revision
    asset = metric("bad", "column.orders.amount")
    asset.properties.expression.expr = expr
    with pytest.raises(ValueError, match=r"use \$\{key\} in expression.expr"):
        store.publish_edit([*store.working.assets, asset])
    assert store.working_revision == before


def test_graph_snapshot_rejects_old_format_without_rewriting(case_graph):
    import json

    store, _, _ = case_graph
    path = store.root / f"{store.working_revision}.json"
    payload = json.loads(path.read_text())
    payload.pop("format_version")
    original = json.dumps(payload)
    path.write_text(original)
    with pytest.raises(ValueError, match="graph format changed; start a new training case"):
        store.read(store.working_revision)
    assert path.read_text() == original
