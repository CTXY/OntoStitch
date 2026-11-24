"""Local packets, real task deltas and metadata/semantic impact boundaries."""

from types import SimpleNamespace

from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.graph.publication import mapped_base
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.edit_contract import Element, IdentityDecision
from cortex.capabilities.graph_merge.consolidation.task_context import (
    LocalTask,
    affected_query_roots,
    build_task_context,
    semantic_review_targets,
)
from cortex.schema import (
    ConflictAlternative,
    GraphNode,
    GraphSpace,
    GraphState,
    SourceReference,
    load_root_schema,
)


def context(nodes=(), edges=()):
    ctx = IntegrationContext(
        state(), state([*catalog(), *nodes], edges), load_root_schema()
    )
    return ctx, ctx.union_state()


def replace(state_, *, nodes=None, edges=None, contracts=None):
    return GraphState(
        graph=GraphSpace(
            nodes=tuple(nodes if nodes is not None else state_.graph.nodes),
            edges=tuple(edges if edges is not None else state_.graph.edges),
            relation_contracts=tuple(
                contracts if contracts is not None else state_.graph.relation_contracts
            ),
        )
    )


def rule(key, intent, action):
    return GraphNode(
        id=key,
        node_type="generation_rule",
        name=key,
        properties={
            "operation_intents": [intent],
            "condition": "Requested object output",
            "action": action,
            "reason": "Supplied query",
            "triggers": {
                "question": {"phrases": ["objects"]},
                "sql": {"columns": [], "operations": []},
            },
        },
    )


def test_local_packet_does_not_expand_read_history_or_other_hub_attributes():
    ctx, current = context([concept("A", "a"), concept("B", "b")])
    ctx.read_ids.update(ctx.nodes)
    a, b = [ctx.input_node_map[k] for k in ["A", "B"]]
    payload = build_task_context(ctx, current, LocalTask("local", "node_assess", {a}))
    assert {n["id"] for n in payload["candidate_nodes"]} == {a}
    assert b not in {n["id"] for n in payload["nodes"]}
    assert payload["reviewed_node_ids"] == [a]
    assert payload["rationale_review_ids"] == []
    assert "identity" not in payload and "candidate_ids" not in payload
    assert "incoming_seeds" not in payload
    assert ctx.candidate == current
    assert not payload["operation_registry"]
    assert ctx.input_node_map["b"] not in {n["id"] for n in payload["physical_facts"]}


def test_review_preserves_new_relation_without_registered_contract():
    ctx, current = context([concept("A", "a"), concept("B", "b")])
    a, b = (ctx.input_node_map[key] for key in ("A", "B"))
    relation = edge("new_relation", a, b, "has_card_name")
    candidate = replace(current, edges=(*current.graph.edges, relation))
    payload = build_task_context(
        ctx, candidate, LocalTask("review", "node_assess", {a}), before=current
    )
    assert relation.model_dump(mode="json") in payload["candidate_relations"]
    assert not any(
        row["contract"]["id"] == "has_card_name"
        for row in payload["active_contracts"]
    )


def test_archived_relation_without_contract_remains_visible_after_removal():
    ctx, current = context(
        [concept("A", "a"), concept("B", "b")],
        [edge("source_relation", "A", "B", "has_card_name")],
    )
    a = ctx.input_node_map["A"]
    relation_id = ctx.input_edge_map["source_relation"]
    candidate = replace(
        current, edges=[e for e in current.graph.edges if e.id != relation_id]
    )
    payload = build_task_context(
        ctx, candidate, LocalTask("review", "node_assess", {a}), before=current
    )
    assert relation_id in {row["id"] for row in payload["relations"]}
    assert not any(row["id"] == "has_card_name" for row in payload["local_contracts"])


def test_complete_group_sources_are_visible_through_alias_lineage():
    ctx, _ = context([concept("A", "a"), concept("B", "b")])
    a, b = [ctx.input_node_map[k] for k in ["A", "B"]]
    decision = IdentityDecision(
        groups=[
            {
                "members": [a, b],
                "meaning": "One referent",
                "reason": "Two representations",
            }
        ]
    )
    ns, es, nm, _ = mapped_base(ctx, decision)
    current = GraphState(
        graph=GraphSpace(nodes=tuple(ns.values()), edges=tuple(es.values()))
    )
    origins = SimpleNamespace(
        node_map=nm,
        edge_map={},
        origin_refs=lambda kind, key: (
            (Element(kind="node", id=a), Element(kind="node", id=b))
            if kind == "node"
            else ()
        ),
    )
    task = LocalTask(
        "group",
        "consolidate",
        {nm[a]},
        groups=[{"canonical_id": nm[a], "members": [a, b]}],
    )
    payload = build_task_context(ctx, current, task, origins=origins)
    assert {a, b} <= {n["id"] for n in payload["nodes"]}
    assert payload["matched_groups"] == task.groups
    assert not payload["missing_source_refs"]
    assert {
        ref["id"] for row in payload["origin_links"] for ref in row["source_refs"]
    } == {a, b}


def test_consumer_context_includes_different_intent_sharing_operand_not_unrelated_rule():
    ctx, current = context(
        [
            concept("A", "a"),
            concept("B", "b"),
            rule("R", "column_binding_choice", "Select representations"),
            rule("Layout", "output_layout", "Order selected content"),
            rule("Other", "column_binding_choice", "Select another meaning"),
        ],
        [
            edge("ra", "R", "A", "applies_to"),
            edge("la", "Layout", "A", "applies_to"),
            edge("ob", "Other", "B", "applies_to"),
        ],
    )
    r, layout, other = [ctx.input_node_map[k] for k in ["R", "Layout", "Other"]]
    payload = build_task_context(ctx, current, LocalTask("choice", "node_assess", {r}))
    assert layout in {n["id"] for n in payload["candidate_nodes"]}
    assert layout in {row["id"] for row in payload["consumer_directory"]}
    assert other not in {n["id"] for n in payload["candidate_nodes"]}
    assert payload["rationale_review_ids"] == [r]
    assert payload["reviewed_node_ids"] == [r]


def test_source_only_edge_packet_does_not_expand_shared_decisions():
    ctx, before = context(
        [
            concept("A", "a"),
            rule("R", "column_binding_choice", "Choose inputs"),
            rule("Layout", "output_layout", "Arrange inputs"),
        ],
        [edge("ra", "R", "A", "applies_to"), edge("la", "Layout", "A", "applies_to")],
    )
    eid = ctx.input_edge_map["ra"]
    changed = [
        e.model_copy(
            update={"sources": (SourceReference(source_id="known", locator="QA"),)}
        )
        if e.id == eid
        else e
        for e in before.graph.edges
    ]
    after = replace(before, edges=changed)
    payload = build_task_context(
        ctx, after, LocalTask("provenance", "reconcile", {eid}), before=before
    )
    layout = ctx.input_node_map["Layout"]
    assert layout not in {n["id"] for n in payload["candidate_nodes"]}
    assert payload["actual_changes"]["nodes"] == {
        "created": [],
        "deleted": [],
        "updated": [],
    }
    assert payload["actual_changes"]["relations"]["updated"] == [eid]
    impact = semantic_review_targets(before, after, {eid})
    assert not impact["targets"] and impact["metadata_only"]
    assert impact["provenance_changed"] and impact["requires_evidence_check"]
    assert not affected_query_roots(before, after)
    assert semantic_review_targets(before, after, {eid}, evidence_changed=True)[
        "targets"
    ] == {ctx.input_node_map["R"], ctx.input_node_map["A"]}


def test_actual_changes_uses_supplied_before_not_original_sources():
    ctx, raw = context([concept("A", "a")])
    aid = ctx.input_node_map["A"]
    before = replace(
        raw,
        nodes=[
            n.model_copy(update={"description": "Already accepted clarification"})
            if n.id == aid
            else n
            for n in raw.graph.nodes
        ],
    )
    after = replace(
        before,
        nodes=[
            n.model_copy(
                update={"sources": (*n.sources, SourceReference(source_id="known"))}
            )
            if n.id == aid
            else n
            for n in before.graph.nodes
        ],
    )
    payload = build_task_context(
        ctx, after, LocalTask("one", "node_assess", {aid}), before=before
    )
    assert payload["actual_changes"]["nodes"]["updated"] == [aid]
    assert payload["deleted_nodes"] == []
    assert (
        payload["candidate_nodes"][0]["description"] == "Already accepted clarification"
    )
    assert not semantic_review_targets(before, after, {aid})["targets"]


def test_explicit_followup_is_complete_without_expanding_unrelated_hub_edges():
    from cortex.capabilities.graph_merge.reconciliation.context import relation_context
    from cortex.capabilities.graph_merge.reconciliation.scope import signal_scope

    ctx, current = context(
        [concept("P", "a"), concept("A", "a"), concept("B", "b"), concept("C", "c")],
        [
            edge("e1", "P", "A", "has_feature"),
            edge("e2", "P", "B", "has_feature"),
            edge("e3", "P", "C", "has_feature"),
            edge("boundary-noise", "A", "C", "has_feature"),
        ],
    )
    required = {ctx.input_edge_map[key] for key in ("e1", "e2")}
    check = {
        "id": "paired-representations",
        "kind": "node_relation_followup",
        "mode": "required",
        "node_ids": [ctx.input_node_map["P"]],
        "edge_ids": sorted(required),
        "required_outcome": "Preserve both evidenced feature roles together.",
    }
    task = LocalTask("followup", "reconcile", {ctx.input_node_map["P"]}, checks=[check])
    payload = relation_context(ctx, current, task)
    selected = signal_scope(ctx, current, task.checks)
    assert {row["id"] for row in payload["edges"]} == required
    assert selected.writable_edge_ids == required
    assert {ctx.input_node_map[key] for key in ("P", "A", "B")} <= {
        row["id"] for row in payload["nodes"]
    }
    assert ctx.input_node_map["C"] not in selected.node_ids


def test_deleted_core_dependency_marks_surviving_owner_and_query_endpoints():
    ctx, before = context(
        [concept("A", "a"), rule("R", "column_binding_choice", "Choose inputs")],
        [edge("ra", "R", "A", "applies_to")],
    )
    eid = ctx.input_edge_map["ra"]
    r, a = [ctx.input_node_map[k] for k in ["R", "A"]]
    after = replace(before, edges=[e for e in before.graph.edges if e.id != eid])
    assert semantic_review_targets(before, after, {eid})["targets"] == {r}
    assert {r, a} <= affected_query_roots(before, after)
    # Business relation changes still invalidate graph queries without forcing
    # every adjacent business object into independent Node Assessment.
    after2 = replace(
        before,
        edges=[
            e.model_copy(update={"edge_type": "has_feature"}) if e.id == eid else e
            for e in before.graph.edges
        ],
    )
    assert {r, a} <= affected_query_roots(before, after2)


def test_deleting_referenced_node_includes_current_consumer():
    dependency = concept("D", "a")
    consumer = concept("C", "b").model_copy(
        update={"properties": {"targets": [["b"]], "references": ["D"]}}
    )
    before = GraphState(graph=GraphSpace(nodes=(dependency, consumer)))
    after = GraphState(graph=GraphSpace(nodes=(consumer,)))
    assert semantic_review_targets(before, after, {"D"})["targets"] == {"C"}


def test_alternative_source_reassignment_is_semantic_not_metadata_only():
    source = SourceReference(source_id="first")
    node = GraphNode(
        id="R",
        node_type="concept",
        name="R",
        merge_status="conflict",
        consolidation_status="semantic_conflict",
        sources=(source, SourceReference(source_id="second")),
        alternatives=(
            ConflictAlternative(
                alternative_id="one",
                overrides={"properties.targets": [["a"]]},
                sources=(source,),
            ),
            ConflictAlternative(
                alternative_id="two",
                overrides={"properties.targets": [["b"]]},
                sources=(SourceReference(source_id="second"),),
            ),
        ),
    )
    before = GraphState(graph=GraphSpace(nodes=(node,)))
    changed = node.model_copy(
        update={
            "sources": (
                SourceReference(source_id="third"),
                SourceReference(source_id="second"),
            ),
            "alternatives": (
                node.alternatives[0].model_copy(
                    update={"sources": (SourceReference(source_id="third"),)}
                ),
                node.alternatives[1],
            ),
        }
    )
    after = replace(before, nodes=[changed])
    impact = semantic_review_targets(before, after, {"R"})
    assert impact["targets"] == {"R"}
    assert not impact["metadata_only"]


def test_empty_scope_and_unknown_targets_never_fall_back_to_whole_graph():
    ctx, current = context([concept("A", "a"), concept("B", "b")])
    empty = build_task_context(ctx, current, LocalTask("empty", "reconcile", set()))
    assert not empty["candidate_nodes"] and not empty["nodes"]
    unknown = build_task_context(
        ctx, current, LocalTask("missing", "reconcile", {"absent"})
    )
    assert not unknown["candidate_nodes"]
    assert unknown["task_scope"]["missing_targets"] == ["absent"]
    assert not unknown["task_scope"]["complete"]


def test_large_hub_defers_optional_adjacency_with_explicit_inspection_pages():
    hub = concept("Hub", "a")
    neighbors = [
        concept(f"N{i:03}", "b").model_copy(update={"description": "Detail " * 100})
        for i in range(160)
    ]
    links = [
        edge(f"e{i:03}", "Hub", n.id, "has_feature") for i, n in enumerate(neighbors)
    ]
    ctx, current = context([hub, *neighbors], links)
    hub_id = ctx.input_node_map["Hub"]
    payload = build_task_context(ctx, current, LocalTask("hub", "reconcile", {hub_id}))
    assert {n["id"] for n in payload["candidate_nodes"]} == {hub_id}
    assert payload["more_candidate_relations"]
    assert payload["candidate_relations_total"] == 160
    scope = payload["task_scope"]
    assert not scope["complete"] and scope["required_units_complete"]
    deferred = next(
        row for row in scope["deferred_context"] if row["view"] == "candidate"
    )
    assert deferred["unloaded_nodes"] == 160
    assert deferred["unloaded_relations"] == 160
    assert len(deferred["nodes"]) == 40 and deferred["directory_has_more"]
    assert deferred["next_query"] == {
        "mode": "relations",
        "view": "candidate",
        "ids": [hub_id],
        "offset": 0,
        "limit": 40,
    }


def test_small_hub_keeps_existing_context_and_large_focus_is_not_split():
    ctx, current = context(
        [concept("Hub", "a"), concept("Leaf", "b")],
        [edge("e", "Hub", "Leaf", "has_feature")],
    )
    hub_id, leaf_id = [ctx.input_node_map[key] for key in ("Hub", "Leaf")]
    small = build_task_context(ctx, current, LocalTask("small", "reconcile", {hub_id}))
    assert {n["id"] for n in small["candidate_nodes"]} == {hub_id, leaf_id}
    assert small["task_scope"]["complete"]
    enormous = next(n for n in current.graph.nodes if n.id == hub_id).model_copy(
        update={"description": "Meaning " * 5000}
    )
    enlarged = replace(
        current, nodes=[enormous, *[n for n in current.graph.nodes if n.id != hub_id]]
    )
    large = build_task_context(ctx, enlarged, LocalTask("large", "reconcile", {hub_id}))
    assert (
        next(n for n in large["candidate_nodes"] if n["id"] == hub_id)["description"]
        == enormous.description
    )
    assert large["task_scope"]["budget_exceeded_by_required_units"]


def test_budget_keeps_explicit_relation_followup_indivisible():
    from cortex.capabilities.graph_merge.engine import review_batches
    from cortex.capabilities.graph_merge.reconciliation.context import relation_context

    descriptions = {f"N{i:03}": f"Supported feature {i}: " + "Evidence " * 100 for i in range(100)}
    neighbors = [
        concept(key, "b").model_copy(update={"description": description})
        for key, description in descriptions.items()
    ]
    ctx, current = context(
        [concept("Hub", "a"), *neighbors],
        [edge(f"e{i:03}", "Hub", node.id, "has_feature") for i, node in enumerate(neighbors)],
    )
    required = {ctx.input_edge_map[key] for key in ("e000", "e001")}
    check = {
        "id": "paired-features",
        "kind": "node_relation_followup",
        "mode": "required",
        "node_ids": [ctx.input_node_map["Hub"], ctx.input_node_map["N000"], ctx.input_node_map["N001"]],
        "edge_ids": sorted(required),
        "required_outcome": "Keep the two nominated feature roles together.",
    }
    # A budget smaller than one full definition cannot split a single explicit
    # obligation or silently truncate its required node bodies.
    batches = review_batches(current, checks=[check], budget=1, signals_only=True)
    assert len(batches) == 1
    targets, checks = batches[0]
    assert checks == [check]
    assert required <= targets
    packet = relation_context(ctx, current, LocalTask("followup-budget", "reconcile", targets, checks=checks))
    assert {row["id"] for row in packet["edges"]} == required
    shown = {row["id"]: row for row in packet["nodes"]}
    for key in ("N000", "N001"):
        assert shown[ctx.input_node_map[key]]["description"] == descriptions[key]
    assert ctx.input_node_map["N002"] not in shown


def test_local_identity_distinctions_preserve_reason_and_original_handles():
    ctx, current = context(
        [concept("A", "a"), concept("B", "b"), concept("C", "c"), concept("D", "c")]
    )
    a, b, c, d = [ctx.input_node_map[key] for key in "ABCD"]
    reason = "Different whole-node scopes, but their shared decision selects incompatible implementations."
    ctx.identity_decision = {
        "groups": [],
        "distinctions": [
            {"members": [a, b], "reason": reason},
            {"members": [c, d], "reason": "Unrelated pair."},
        ],
    }
    origins = SimpleNamespace(
        node_map={a: a, b: b},
        edge_map={},
        origin_refs=lambda kind, key: (Element(kind=kind, id=key),),
    )
    payload = build_task_context(
        ctx, current, LocalTask("local", "reconcile", {a}), origins
    )
    assert len(payload["identity_distinctions"]) == 1
    distinction = payload["identity_distinctions"][0]
    assert distinction["members"] == [a, b]
    assert distinction["reason"] == reason
    assert distinction["current_refs"] == {
        a: [{"kind": "node", "id": a}],
        b: [{"kind": "node", "id": b}],
    }
    assert "groups" not in payload
    assert b not in {n["id"] for n in payload["candidate_nodes"]}


def test_identity_distinction_follows_recorded_alias_without_rewriting_members():
    ctx, _ = context([concept("A", "a"), concept("B", "b"), concept("Peer", "c")])
    a, b, peer = [ctx.input_node_map[key] for key in ("A", "B", "Peer")]
    current = state([*catalog(), concept("Combined", "a"), concept("Remaining", "b")])
    ctx.identity_decision = {
        "groups": [],
        "distinctions": [
            {
                "members": [a, peer],
                "reason": "Keep the source-level distinction visible.",
            }
        ],
    }
    origins = SimpleNamespace(
        node_map={a: "Combined", b: "Combined", peer: "Remaining"},
        edge_map={},
        origin_refs=lambda kind, key: tuple(
            Element(kind="node", id=k)
            for k in ({"Combined": [a, b], "Remaining": [peer]}.get(key, []))
        ),
    )
    payload = build_task_context(
        ctx, current, LocalTask("alias", "reconcile", {"Combined"}), origins
    )
    row = payload["identity_distinctions"][0]
    assert row["members"] == [a, peer]
    assert row["current_refs"][a] == [{"kind": "node", "id": "Combined"}]
    assert row["current_refs"][peer] == [{"kind": "node", "id": "Remaining"}]
