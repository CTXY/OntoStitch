import pytest
from conftest import make_rule

from cortex.capabilities.sql_training.graph.mutations import (
    ChangeGroup,
    GraphEdit,
    apply_edit,
    normalize_groups,
)
from cortex.capabilities.sql_training.models import NodeAsset


def edit(store, database, asset, operation="create", edit_id="e1"):
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
            }
        ),
        database=database,
        source_id="case",
    )


def test_complete_physical_graph_and_failed_edit_are_immutable(case_graph):
    store, database, _ = case_graph
    old = store.working
    assert {"table.unused", "column.unused.code"} <= {a.id for a in old.assets}
    with pytest.raises(ValueError, match="unresolved"):
        edit(store, database, make_rule(references=["column.missing.nope"]))
    assert store.working_revision == old.revision
    assert store.read(old.revision) == old


def test_cannot_edit_physical_or_delete_retained_nodes(case_graph):
    store, database, _ = case_graph
    table = next(a for a in store.working.assets if a.id == "table.orders")
    with pytest.raises(ValueError, match="physical"):
        edit(store, database, table, "update")
    edit(store, database, make_rule())
    store.retain_direct()
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
    store.retain_direct()
    baseline = store.accepted_revision
    m2 = edit(store, database, make_rule(action="m2"), "update", "e2")
    store.promote(
        assessed_revision=m2.revision,
        accepted_before=baseline,
        member_ids=[make_rule().id],
    )
    accepted = store.accepted_revision
    edit(store, database, make_rule(action="m3"), "update", "e3")
    retained = store.retain()
    assert retained.semantic_assets[0].properties.action == "m2"
    assert retained.revision == accepted
    assert retained.revision != baseline


def test_acceptance_uses_complete_assessed_content_not_last_edit(case_graph):
    store, database, _ = case_graph
    edit(store, database, make_rule(action="m1"))
    store.retain_direct()
    accepted = store.accepted_revision
    edit(store, database, make_rule(action="m2"), "update", "e2")
    current = store.working.semantic_assets[0].model_copy(update={"aliases": ["alias"]})
    snap = edit(store, database, current, "update", "e3")
    promoted = store.promote(
        assessed_revision=snap.revision,
        accepted_before=accepted,
        member_ids=[current.id],
    )
    assert promoted.semantic_assets[0].properties.action == "m2"
    assert promoted.semantic_assets[0].aliases == ["alias"]
    with pytest.raises(ValueError, match="stale"):
        store.promote(
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
                "targets": [
                    {
                        "sql_expression": "orders.region = 'North'",
                        "references": ["column.orders.region"],
                    }
                ]
            },
        }
    )
    snap = edit(store, database, concept)
    assert any(a.id.startswith("dimension_value.") for a in snap.assets)
    assert store.read(old.revision) == old
    retained = store.retain()
    assert not retained.semantic_assets
    assert any(a.id.startswith("dimension_value.") for a in retained.assets)


def test_edit_dedup_does_not_restore_old_head(case_graph):
    store, database, _ = case_graph
    initial = store.working_revision
    request = GraphEdit.model_validate(
        {
            "edit_id": "same",
            "expected_revision": initial,
            "operations": [
                {
                    "operation": "create",
                    "node_id": make_rule().id,
                    "asset": make_rule().model_dump(mode="json"),
                }
            ],
        }
    )
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    first = tool.invoke({"edit": request.model_dump(mode="json")})
    last = edit(store, database, make_rule(action="new"), "update", "next")
    assert (
        tool.invoke({"edit": request.model_dump(mode="json")})["applied_revision"]
        == first["applied_revision"]
    )
    assert store.working_revision == last.revision
    changed = request.model_copy(update={"expected_revision": last.revision})
    with pytest.raises(ValueError, match="different"):
        tool.invoke({"edit": changed.model_dump(mode="json")})
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
    request = GraphEdit.model_validate(
        {
            "edit_id": "lost",
            "expected_revision": base,
            "operations": [
                {
                    "operation": "create",
                    "node_id": make_rule().id,
                    "asset": make_rule().model_dump(mode="json"),
                }
            ],
        }
    )
    from test_agent_runtime import session_for

    from cortex.capabilities.sql_training.learning.tools import build_tools

    tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    tool.invoke({"edit": request.model_dump(mode="json")})
    store.restore(base, base)
    with pytest.raises(ValueError, match="discarded"):
        tool.invoke({"edit": request.model_dump(mode="json")})
    assert store.working_revision == base
    fresh_tool = next(
        t for t in build_tools(session_for(case_graph)) if t.name == "edit_graph"
    )
    fresh_tool.invoke({"edit": request.model_dump(mode="json")})
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
                "targets": [
                    {
                        "sql_expression": "orders.region = 'North'",
                        "references": ["column.orders.region"],
                    }
                ]
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
    store.promote(
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
                "references": [reference],
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
        store.promote(
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
    with pytest.raises(ValueError, match="unverified"):
        edit(
            store,
            database,
            make_rule(references=["dimension_value.orders.region.Invented"]),
        )
