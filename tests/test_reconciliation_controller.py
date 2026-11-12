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
    ReconciliationBatch,
    RepairUnit,
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
        result = (
            self.writer(ctx, payload)
            if phase == "reconcile"
            else self.assessor(payload)
        )
        IncrementalIntegrationAgent._validate(result, ctx, payload, phase)
        return result


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
    with pytest.raises(ValueError, match="Conclude each active inspection"):
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
    assert retry["routine_review_ids"] == [b]
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


def test_actual_role_change_requires_review_of_business_endpoints():
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
        update={"graph": current.graph.model_copy(update={"edges": (changed,)})}
    )
    controller = ReconciliationController(engine, "controlled")
    ids = controller._review_ids(
        current, candidate, LocalTask("role", "reconcile", {old.id}), []
    )
    assert {old.from_node_id, old.to_node_id} <= ids


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
