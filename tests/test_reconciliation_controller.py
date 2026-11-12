"""Controlled semantic transactions; no domain-specific repair code or API calls."""

import pytest
from test_graph_integration import catalog, concept, edge, state

from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.context import IntegrationContext
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.incremental import (
    IncrementalGraphIntegrationEngine,
)
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.reconciliation import (
    ReconciliationController,
)
from cortex.capabilities.graph_merge.integration.reconciliation_units import (
    CheckedUnit as RepairUnit,
)
from cortex.capabilities.graph_merge.integration.reconciliation_units import (
    ReconciliationBatch,
    group_units,
)
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.incremental import (
    BatchNodeReview,
    IncrementalIntegrationAgent,
    NodeReview,
    UnitNodeReview,
)
from cortex.schema import load_root_schema


def engine_for(agent, source=None, max_rounds=1):
    source = source or state(
        [*catalog(), concept("A", "a"), concept("B", "b"), concept("C", "c")]
    )
    ctx = IntegrationContext(state(), source, load_root_schema())
    engine = IncrementalGraphIntegrationEngine(agent, max_rounds=max_rounds)
    engine.ctx, engine.current = ctx, ctx.union_state()
    engine.lineage, engine.history, engine.reviewed_sources = LineageIndex(ctx), [], {}
    engine._revision, engine.open_tasks = 0, []
    return engine


def review_all(payload, failures=()):
    return BatchNodeReview(
        results=tuple(
            UnitNodeReview(
                unit_id=u["unit_id"],
                review=NodeReview(
                    verdict="revise" if u["unit_id"] in failures else "accept",
                    issues=("Unsupported replacement meaning",)
                    if u["unit_id"] in failures
                    else (),
                    decision_checks=tuple(
                        {"node_id": k, "independent_decisions": ["One decision"]}
                        for k in u["decision_review_ids"]
                    ),
                    rationale_checks=tuple(
                        {
                            "node_id": k,
                            "supported": True,
                            "explanation": "Supported by the supplied case",
                        }
                        for k in u["rationale_review_ids"]
                    ),
                ),
            )
            for u in payload["review_units"]
        )
    )


class ScriptedAgent:
    def __init__(self, writer, assessor=None):
        self.writer, self.assessor = writer, assessor or review_all
        self.calls = []

    def invoke(self, phase, ctx, payload):
        self.calls.append((phase, payload))
        if phase == "reconcile":
            # Fixture builders select nodes; the wire protocol answers only
            # the single inspection registry, just like a real model result.
            local = {**payload, "routine_review_ids": routine_ids(payload)}
            result = self.writer(ctx, local)
            units = []
            for unit in result.units:
                found = {c.inspection_id for c in unit.patch.inspection_checks}
                extra = tuple(
                    {
                        "inspection_id": key,
                        "conclusion": "keep",
                        "reason": "Node meaning checked against complete inputs",
                    }
                    for key in getattr(unit, "checked_node_ids", ())
                    if key not in found
                )
                patch = BatchResult.model_validate(
                    {
                        **unit.patch.model_dump(mode="json"),
                        "inspection_checks": [
                            c.model_dump(mode="json")
                            for c in unit.patch.inspection_checks
                        ]
                        + list(extra),
                    }
                )
                units.append({"unit_id": unit.unit_id, "patch": patch})
            result = ReconciliationBatch(
                units=tuple(units), coupled_units=result.coupled_units
            )
        else:
            result = self.assessor(payload)
        IncrementalIntegrationAgent._validate(result, ctx, payload, phase)
        return result


def routine_ids(payload):
    return [
        c["id"] for c in payload["inspection_signals"] if c["kind"] == "node_content"
    ]


def no_change(ctx, payload):
    return ReconciliationBatch(
        units=(
            RepairUnit(
                unit_id="checked",
                checked_node_ids=tuple(payload["routine_review_ids"]),
                patch=BatchResult(
                    inspection_checks=tuple(
                        {
                            "inspection_id": c["id"],
                            "conclusion": "keep",
                            "reason": "Complete facts are compatible",
                            "decision_relation": "compatible"
                            if c["kind"]
                            in {"related_decisions", "source_selection_variants"}
                            else None,
                        }
                        for c in payload["inspection_signals"]
                    )
                ),
            ),
        )
    )


def test_complete_ordinary_no_change_uses_no_assessment():
    agent = ScriptedAgent(no_change)
    engine = engine_for(agent)
    before = engine.current
    ReconciliationController(engine, "controlled").run(first=True)
    assert [p for p, _ in agent.calls] == ["reconcile"]
    assert engine.current == before
    assert engine.history[0]["assessment"] == "not_required"


def test_unanswered_checks_cannot_be_recorded_as_no_change(monkeypatch):
    from cortex.capabilities.graph_merge.integration import reconciliation as module

    agent = ScriptedAgent(
        lambda ctx, p: ReconciliationBatch(
            units=(
                RepairUnit(
                    unit_id="empty",
                    checked_node_ids=tuple(p["routine_review_ids"]),
                    patch=BatchResult(),
                ),
            )
        )
    )
    engine = engine_for(agent)
    a = engine.ctx.input_node_map["A"]
    monkeypatch.setattr(
        module,
        "inspection_signals",
        lambda *args, **kw: [{"id": "required", "kind": "triangle", "node_ids": [a]}],
    )
    with pytest.raises(ValueError, match="supplied registry"):
        ReconciliationController(engine, "controlled").run(first=True)
    assert not engine.history


def test_explicit_unresolved_decision_is_assessed_even_without_edits(monkeypatch):
    from cortex.capabilities.graph_merge.integration import reconciliation as module

    agent = ScriptedAgent(no_change)
    engine = engine_for(agent)
    a = engine.ctx.input_node_map["A"]
    monkeypatch.setattr(
        module,
        "inspection_signals",
        lambda *args, **kw: [
            {"id": "choice", "kind": "source_selection_variants", "node_ids": [a]}
        ],
    )
    ReconciliationController(engine, "controlled").run(first=True)
    assert [p for p, _ in agent.calls] == ["reconcile", "node_assess"]


def test_only_failed_unit_is_regenerated_and_reassessed():
    def writer(ctx, p):
        units = []
        for key in p["routine_review_ids"]:
            node = next(n for n in ctx.candidate.graph.nodes if n.id == key)
            fixed = node.model_copy(update={"description": "Supported revised meaning"})
            units.append(
                RepairUnit(
                    unit_id=key,
                    checked_node_ids=(key,),
                    patch=BatchResult(
                        edits=(
                            {
                                "operation": "update_node",
                                "node": fixed.model_dump(mode="json"),
                            },
                        )
                    ),
                )
            )
        return ReconciliationBatch(units=tuple(units))

    assessment_count = 0

    def assessor(p):
        nonlocal assessment_count
        assessment_count += 1
        return review_all(p, failures=(b,) if assessment_count == 1 else ())

    agent = ScriptedAgent(writer, assessor)
    engine = engine_for(agent)
    b = engine.ctx.input_node_map["B"]
    ReconciliationController(engine, "controlled").run(first=True)
    assert [p for p, _ in agent.calls] == [
        "reconcile",
        "node_assess",
        "reconcile",
        "node_assess",
    ]
    retry = agent.calls[2][1]
    assert routine_ids(retry) == [b]
    assert [u["unit_id"] for u in retry["retry_units"]] == [b]
    assert [u["unit_id"] for u in agent.calls[3][1]["review_units"]] == [b]
    assert len(engine.history) == 3


def test_rejected_patch_cannot_escape_assessment_by_returning_empty_patch():
    count = 0

    def writer(ctx, p):
        nonlocal count
        count += 1
        if count > 1:
            return no_change(ctx, p)
        node = next(
            n for n in ctx.candidate.graph.nodes if n.id == p["routine_review_ids"][0]
        )
        node = node.model_copy(update={"description": "Unsupported meaning"})
        return ReconciliationBatch(
            units=(
                RepairUnit(
                    unit_id="bad",
                    checked_node_ids=(node.id,),
                    patch=BatchResult(
                        edits=(
                            {
                                "operation": "update_node",
                                "node": node.model_dump(mode="json"),
                            },
                        )
                    ),
                ),
            )
        )

    agent = ScriptedAgent(
        writer,
        lambda p: review_all(
            p, failures=tuple(u["unit_id"] for u in p["review_units"])
        ),
    )
    engine = engine_for(agent)
    a = engine.ctx.input_node_map["A"]
    before = engine.current
    with pytest.raises(IntegrationIncomplete):
        ReconciliationController(engine, "controlled").run(initial_targets={a})
    assert [p for p, _ in agent.calls] == [
        "reconcile",
        "node_assess",
        "reconcile",
        "node_assess",
    ]
    assert engine.current == before and not engine.history


def test_shared_read_only_parent_is_not_a_dependency_but_new_endpoint_is():
    base = state(
        [*catalog(), concept("School", "a"), concept("A", "b"), concept("B", "c")]
    )
    left = RepairUnit(
        unit_id="left",
        checked_node_ids=("A",),
        patch=BatchResult(
            edits=(
                {
                    "operation": "create_edge",
                    "edge": edge("ea", "A", "School", "is_a").model_dump(mode="json"),
                },
            )
        ),
    )
    right = RepairUnit(
        unit_id="right",
        checked_node_ids=("B",),
        patch=BatchResult(
            edits=(
                {
                    "operation": "create_edge",
                    "edge": edge("eb", "B", "School", "is_a").model_dump(mode="json"),
                },
            )
        ),
    )
    assert len(group_units([left, right], base)) == 2
    create = RepairUnit(
        unit_id="create",
        patch=BatchResult(
            edits=(
                {
                    "operation": "create_node",
                    "node": concept("School", "a").model_dump(mode="json"),
                },
            )
        ),
    )
    assert len(group_units([create, left, right], state())) == 1


def test_actual_role_change_reviews_edge_with_endpoints_as_context():
    source = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [edge("role", "A", "B", "is_a")],
    )
    engine = engine_for(None, source)
    current = engine.current
    old = next(
        e for e in current.graph.edges if e.id == engine.ctx.input_edge_map["role"]
    )
    changed = old.model_copy(
        update={"from_node_id": old.to_node_id, "to_node_id": old.from_node_id}
    )
    candidate = current.model_copy(
        update={
            "graph": current.graph.model_copy(
                update={
                    "edges": tuple(
                        changed if e.id == old.id else e for e in current.graph.edges
                    )
                }
            )
        }
    )
    controller = ReconciliationController(engine, "controlled")
    ids = controller._review_ids(
        current, candidate, LocalTask("role", "reconcile", {old.id}), []
    )
    assert ids == {old.id}
    packet = controller._packet(
        LocalTask("edge-review", "node_assess", ids), candidate, before=current
    )
    assert packet["reviewed_node_ids"] == []
    assert {old.from_node_id, old.to_node_id} <= {
        n["id"] for n in packet["candidate_nodes"]
    }


def test_deletion_without_correspondence_never_changes_graph_or_lineage():
    engine = engine_for(None)
    a = engine.ctx.input_node_map["A"]
    controller = ReconciliationController(engine, "controlled")
    before, lineage = engine.current, engine.lineage.snapshot()
    unit = RepairUnit(
        unit_id="delete",
        patch=BatchResult(edits=({"operation": "delete_node", "node_id": a},)),
    )
    with pytest.raises(ValueError, match="disposition"):
        controller._stage(
            before, engine.lineage, LocalTask("delete", "reconcile", {a}), unit
        )
    assert engine.current == before and engine.lineage.snapshot() == lineage


def test_commit_refreshes_and_removes_obsolete_signal(monkeypatch):
    from cortex.capabilities.graph_merge.integration import reconciliation as module

    agent = ScriptedAgent(no_change)
    engine = engine_for(agent)
    a = engine.ctx.input_node_map["A"]
    b = engine.ctx.input_node_map["B"]
    controller = ReconciliationController(engine, "controlled", packet_budget=1)
    visits = []

    def signals(*args, **kwargs):
        return (
            []
            if visits
            else [{"id": k, "kind": "example", "node_ids": [k]} for k in (a, b)]
        )

    def execute(task, payload):
        visits.append(task)
        # The completed repair also covers B and removes the former pattern.
        return task.targets | {a, b}, set()

    monkeypatch.setattr(module, "inspection_signals", signals)
    monkeypatch.setattr(controller, "_execute", execute)
    controller.run(first=True)
    assert len(visits) == 2  # B is covered; only the independent C still needs review.
    assert b not in visits[1].targets


def test_feedback_extension_becomes_an_explicit_retry_review_target():
    first = True

    def writer(ctx, p):
        units = []
        for key in p["routine_review_ids"]:
            old = next(n for n in ctx.candidate.graph.nodes if n.id == key)
            new = old.model_copy(update={"description": "Supported revised content"})
            units.append(
                RepairUnit(
                    unit_id=key,
                    checked_node_ids=(key,),
                    patch=BatchResult(
                        edits=(
                            {
                                "operation": "update_node",
                                "node": new.model_dump(mode="json"),
                            },
                        )
                    ),
                )
            )
        return ReconciliationBatch(
            units=tuple(units),
            coupled_units=(tuple(u.unit_id for u in units),) if len(units) > 1 else (),
        )

    def assessor(p):
        nonlocal first
        if first:
            first = False
            unit = p["review_units"][0]
            return BatchNodeReview(
                results=(
                    UnitNodeReview(
                        unit_id=unit["unit_id"],
                        review=NodeReview(
                            verdict="revise",
                            issues=("The directly related B also requires correction",),
                            issue_refs=({"kind": "node", "id": b},),
                        ),
                    ),
                )
            )
        return review_all(p)

    agent = ScriptedAgent(writer, assessor)
    engine = engine_for(agent)
    a, b = (engine.ctx.input_node_map[k] for k in ("A", "B"))
    ReconciliationController(engine, "feedback-extension").run(initial_targets={a})
    retry = next(
        p for phase, p in agent.calls if phase == "reconcile" and p.get("retry_units")
    )
    assert set(routine_ids(retry)) == {a, b}
    assert [phase for phase, _ in agent.calls] == [
        "reconcile",
        "node_assess",
        "reconcile",
        "node_assess",
    ]


def test_abstract_no_change_cannot_bypass_the_hard_relation_contract():
    source = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [edge("abstract", "A", "B", "has_attribute")],
    )
    agent = ScriptedAgent(no_change)
    engine = engine_for(agent, source)
    with pytest.raises(ValueError, match="abstract|specializ|concrete"):
        ReconciliationController(engine, "abstract").run(first=True)
    assert not engine.history


def test_mechanical_coupling_rejects_empty_or_unknown_groups():
    unit = RepairUnit(unit_id="one", patch=BatchResult())
    with pytest.raises(ValueError, match="two distinct"):
        group_units([unit], state(), coupled=[()])
    with pytest.raises(ValueError, match="Unknown"):
        group_units([unit], state(), coupled=[("one", "unknown")])


def test_wire_schema_has_one_coverage_list_and_no_parallel_node_ack_field():
    schema = ReconciliationBatch.model_json_schema()
    assert set(schema["$defs"]["RepairUnit"]["properties"]) == {"unit_id", "patch"}
    agent = ScriptedAgent(no_change)
    engine = engine_for(agent)
    ReconciliationController(engine, "single-registry").run(first=True)
    packet = agent.calls[0][1]
    assert "routine_review_ids" not in packet
    assert {s["id"] for s in packet["inspection_signals"]} == {
        engine.ctx.input_node_map[k] for k in ("A", "B", "C")
    }


def test_peripheral_edges_do_not_reopen_checked_node_definitions():
    from cortex.capabilities.graph_merge.integration.reconciliation_scope import (
        definition_stamp,
    )

    engine = engine_for(None)
    a, b = (engine.ctx.input_node_map[k] for k in ("A", "B"))
    before = engine.current
    after = before.model_copy(
        update={
            "graph": before.graph.model_copy(
                update={"edges": (*before.graph.edges, edge("role", a, b, "is_a"))}
            )
        }
    )
    assert definition_stamp(before, a) == definition_stamp(after, a)
    controller = ReconciliationController(engine, "scope")
    controller.checked_nodes[a] = definition_stamp(before, a)
    packet = controller._packet(
        LocalTask(
            "role",
            "reconcile",
            {a, b},
            checks=[{"id": "pattern", "kind": "triangle", "node_ids": [a, b]}],
        ),
        after,
    )
    assert a not in routine_ids(packet) and b in routine_ids(packet)
    changed = next(n for n in after.graph.nodes if n.id == a).model_copy(
        update={"description": "New meaning"}
    )
    revised = after.model_copy(
        update={
            "graph": after.graph.model_copy(
                update={
                    "nodes": tuple(
                        changed if n.id == a else n for n in after.graph.nodes
                    )
                }
            )
        }
    )
    assert definition_stamp(before, a) != definition_stamp(revised, a)


def test_binding_changes_still_invalidate_node_definition_coverage():
    from cortex.capabilities.graph_merge.integration.reconciliation_scope import (
        definition_stamp,
    )

    engine = engine_for(None)
    a = engine.ctx.input_node_map["A"]
    initial = engine.current
    target = next(n for n in initial.graph.nodes if n.id == a).properties["targets"][0][
        0
    ]
    binding = edge("binding", a, target, "grounded_on")
    before = initial.model_copy(
        update={
            "graph": initial.graph.model_copy(
                update={"edges": (*initial.graph.edges, binding)}
            )
        }
    )
    after = before.model_copy(
        update={
            "graph": before.graph.model_copy(
                update={
                    "edges": tuple(e for e in before.graph.edges if e.id != binding.id)
                }
            )
        }
    )
    assert definition_stamp(before, a) != definition_stamp(after, a)


def test_same_job_coverage_survives_controller_restart_but_not_code_change():
    agent = ScriptedAgent(no_change)
    engine = engine_for(agent)
    ReconciliationController(engine, "run").run(first=True)
    count = len(agent.calls)
    ReconciliationController(engine, "run").run(first=True)
    assert len(agent.calls) == count
    engine.reconciliation_coverage["implementation_version"] = "older-code"
    ReconciliationController(engine, "run").run(first=True)
    assert len(agent.calls) == count + 1


def test_rejected_edge_only_patch_cannot_escape_review_with_a_noop():
    source = state(
        [*catalog(), concept("A", "a"), concept("B", "b")],
        [edge("role", "A", "B", "is_a")],
    )
    first = True

    def writer(ctx, p):
        nonlocal first
        patch = BatchResult(
            inspection_checks=tuple(
                {"inspection_id": s["id"], "conclusion": "keep", "reason": "Checked"}
                for s in p["inspection_signals"]
            )
        )
        if first:
            first = False
            old = next(e for e in ctx.candidate.graph.edges if e.id == role)
            revised = old.model_copy(
                update={"from_node_id": old.to_node_id, "to_node_id": old.from_node_id}
            )
            patch = BatchResult.model_validate(
                {
                    **patch.model_dump(mode="json"),
                    "edits": [
                        {
                            "operation": "update_edge",
                            "edge": revised.model_dump(mode="json"),
                        }
                    ],
                }
            )
        return ReconciliationBatch(units=({"unit_id": "role", "patch": patch},))

    agent = ScriptedAgent(writer, lambda p: review_all(p, failures=("role",)))
    engine = engine_for(agent, source)
    role = engine.ctx.input_edge_map["role"]
    controller = ReconciliationController(engine, "edge-retry")
    with pytest.raises(IntegrationIncomplete):
        controller._execute(LocalTask("edge", "reconcile", {role}), {})
    assert [p for p, _ in agent.calls] == [
        "reconcile",
        "node_assess",
        "reconcile",
        "node_assess",
    ]
    assert all(
        p["review_units"][0]["reviewed_edge_ids"] == [role]
        for phase, p in agent.calls
        if phase == "node_assess"
    )


def test_conflict_reference_error_names_owner_not_a_different_semantic_decision():
    from cortex.application.ontology_graph import GraphManager
    from cortex.schema import NodeContent, SourceReference

    root, _ = GraphManager.compact_conflict(
        concept("Root", "a"),
        [
            (
                "left",
                NodeContent(
                    name="Shared term",
                    description=None,
                    aliases=[],
                    tags=[],
                    properties={"targets": [["a"]]},
                ),
                (SourceReference(source_id="left"),),
            ),
            (
                "right",
                NodeContent(
                    name="Shared term",
                    description=None,
                    aliases=[],
                    tags=[],
                    properties={"targets": [["b"]]},
                ),
                (SourceReference(source_id="right"),),
            ),
        ],
        consolidation_status="semantic_conflict",
        conflict_points=[
            {
                "point_id": "binding",
                "aspect": "Implementation",
                "description": "Competing bindings",
                "choices": [
                    {"alternative_ids": ["left"], "statement": "First binding"},
                    {"alternative_ids": ["right"], "statement": "Second binding"},
                ],
            }
        ],
    )

    def writer(ctx, p):
        return ReconciliationBatch(
            units=(
                {
                    "unit_id": "choice",
                    "patch": {
                        "inspection_checks": [
                            {
                                "inspection_id": check["id"],
                                "conclusion": "keep",
                                "reason": "The complete alternatives remain",
                                "decision_relation": "conflict",
                                "result_ids": ["left", "right"],
                            }
                            for check in p["inspection_signals"]
                        ]
                    },
                },
            )
        )

    source = state([*catalog(), root])
    ctx = IntegrationContext(source, state(), load_root_schema())
    engine = IncrementalGraphIntegrationEngine(ScriptedAgent(writer))
    engine.ctx, engine.current = ctx, ctx.union_state()
    engine.lineage, engine.history, engine.reviewed_sources = LineageIndex(ctx), [], {}
    engine._revision, engine.open_tasks = 0, []
    owner = "Root"
    task = LocalTask(
        "choice",
        "reconcile",
        {owner},
        checks=[
            {
                "id": "choice-check",
                "kind": "source_selection_variants",
                "node_ids": [owner],
            }
        ],
    )
    with pytest.raises(ValueError, match="OWNER NODE IDs") as error:
        ReconciliationController(engine, "owners")._execute(task, {})
    assert owner in str(error.value) and "Fix" in str(error.value)
    assert not engine.history


def test_sqlite_resume_keeps_same_job_check_coverage_without_more_model_calls(tmp_path):
    from test_incremental_engine import IdentityOnlyAgent

    from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore

    source = state([*catalog(), concept("A", "a"), concept("B", "a")])
    first = IncrementalGraphIntegrationEngine(IdentityOnlyAgent())
    first.workspace_path = tmp_path / "workspace.sqlite"
    first.merge(state(), source, load_root_schema(), run_id="original")
    saved = WorkspaceStore(first.workspace_path).load("original")
    assert saved["reconciliation_coverage"]["nodes"]
    agent = IdentityOnlyAgent()
    resumed = IncrementalGraphIntegrationEngine(agent)
    resumed.resume_snapshot = saved
    result = resumed.merge(state(), source, load_root_schema(), run_id="resumed")
    assert agent.phases == []
    assert len(result.node_mappings) == len(source.graph.nodes)
