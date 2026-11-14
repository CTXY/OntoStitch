"""Semantic roles remain separate while accepted workspace changes are incremental."""

from test_graph_integration import catalog, concept, state

from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.incremental import (
    IncrementalGraphIntegrationEngine,
    review_batches,
)
from cortex.capabilities.graph_merge.integration.models import IdentityDecision
from cortex.capabilities.graph_merge.llm.incremental import NodeReview
from cortex.schema import load_root_schema


class IdentityOnlyAgent:
    def __init__(self):
        self.phases = []

    def invoke(self, phase, ctx, payload):
        self.phases.append(phase)
        if phase == "identity":
            result = IdentityDecision.model_validate(
                {
                    "groups": [
                        {
                            "members": [
                                ctx.input_node_map["A"],
                                ctx.input_node_map["B"],
                            ],
                            "meaning": "One evidenced referent",
                            "reason": "Same referent and binding",
                        }
                    ]
                }
            )
        elif phase == "node_assess":
            result = NodeReview(verdict="accept")
        else:
            result = BatchResult()
        callback = getattr(ctx, "output_validator", None)
        if callback:
            callback(result)
        return result


def test_identity_content_assessment_and_structural_review_publish_once(tmp_path):
    agent = IdentityOnlyAgent()
    engine = IncrementalGraphIntegrationEngine(agent)
    engine.workspace_path = tmp_path / "workspace.sqlite"
    original = state([*catalog(), concept("A", "a"), concept("B", "a")])
    result = engine.merge(state(), original, load_root_schema(), run_id="serial")
    assert len([n for n in result.state.graph.nodes if n.node_type == "concept"]) == 1
    assert agent.phases == ["identity", "consolidate", "node_assess", "reconcile"]
    assert len(engine.history) == 2
    assert engine.last_session["revision"] == 2


def test_first_structural_pass_covers_unmatched_nodes_without_signals():
    original = state([*catalog(), concept("A", "a"), concept("B", "b")])
    groups = review_batches(original, checks=())
    covered = set().union(*(ids for ids, _ in groups))
    assert {"A", "B"} <= covered


def test_no_edit_decision_judgment_skips_independent_assessment():
    from test_graph_integration import edge

    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
    from cortex.capabilities.graph_merge.integration.task_context import LocalTask
    from cortex.schema import GraphNode

    rules = [
        GraphNode(
            id=k,
            node_type="generation_rule",
            name=k,
            properties={
                "condition": "A shared request",
                "action": action,
                "reason": "Supplied Gold implementation",
                "operation_intents": ["column_binding_choice"],
                "triggers": {
                    "question": {"phrases": ["shared request"]},
                    "sql": {"columns": [], "operations": []},
                },
            },
        )
        for k, action in [
            ("r1", "Choose all representations"),
            ("r2", "Choose the first representation"),
        ]
    ]
    original = state(
        [*catalog(), concept("A", "a"), *rules],
        [edge("e1", "r1", "A", "applies_to"), edge("e2", "r2", "A", "applies_to")],
    )
    ctx = IntegrationContext(state(), original, load_root_schema())
    ids = [ctx.input_node_map[k] for k in ["r1", "r2"]]
    signal = {"id": "related", "kind": "related_decisions", "node_ids": ids}
    phases = []

    class Agent:
        def invoke(self, phase, context, payload):
            phases.append(phase)
            if phase == "reconcile":
                result = BatchResult.model_validate(
                    {
                        "inspection_checks": [
                            {
                                "inspection_id": "related",
                                "conclusion": "keep",
                                "decision_relation": "different_decision",
                                "reason": "A no-edit compatibility assertion requiring independent review.",
                            }
                        ]
                    }
                )
                context.output_validator(result)
                return result
            assert {s["id"] for s in payload["inspection_signals"]} == {"related"}
            assert set(ids) <= set(payload["reviewed_node_ids"])
            return NodeReview(
                verdict="revise",
                issues=("Competing implementations still need complete alternatives.",),
            )

    engine = IncrementalGraphIntegrationEngine(Agent(), max_rounds=0)
    engine.ctx = ctx
    engine.current = ctx.union_state()
    engine.lineage = LineageIndex(ctx)
    engine._revision = 0
    engine.reviewed_sources = {}
    engine.history = []
    before = engine.current
    engine._run_task(
        LocalTask("decision", "reconcile", set(ids), checks=[signal]), "unit"
    )
    assert phases == ["reconcile"]
    assert engine.current == before and len(engine.history) == 1
    assert engine.history[0]["assessment"] == "skipped_no_change"


def test_semantic_deletion_is_reviewed_even_without_a_surviving_changed_node():
    import pytest
    from test_graph_integration import edge

    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
    from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
    from cortex.capabilities.graph_merge.integration.task_context import LocalTask
    from cortex.schema import GraphNode

    rule = GraphNode(
        id="r",
        node_type="generation_rule",
        name="Presentation choice",
        properties={
            "condition": "A shared request",
            "action": "Present the chosen components separately",
            "reason": "Gold uses separate projected columns",
            "operation_intents": ["output_field_layout"],
            "triggers": {
                "question": {"phrases": ["requested components"]},
                "sql": {"columns": [], "operations": []},
            },
        },
    )
    incoming = state(
        [*catalog(), concept("A", "a"), rule], [edge("er", "r", "A", "applies_to")]
    )
    ctx = IntegrationContext(state(), incoming, load_root_schema())
    r, e = ctx.input_node_map["r"], ctx.input_edge_map["er"]
    phases = []

    class Agent:
        def invoke(self, phase, context, payload):
            phases.append(phase)
            if phase == "reconcile":
                result = BatchResult.model_validate(
                    {
                        "edits": [
                            {"operation": "delete_edge", "edge_id": e},
                            {"operation": "delete_node", "node_id": r},
                        ],
                        "changes": [
                            {
                                "before": [
                                    {"kind": "node", "id": r},
                                    {"kind": "edge", "id": e},
                                ],
                                "after": [],
                                "explanation": "An unsupported deletion rationale to be independently checked.",
                            }
                        ],
                    }
                )
                context.output_validator(result)
                return result
            assert phase == "graph_assess"
            assert any(n["id"] == r for n in payload["before_nodes"])
            return NodeReview(
                verdict="revise",
                issues=("The independent supported presentation choice was erased.",),
            )

    engine = IncrementalGraphIntegrationEngine(Agent(), max_rounds=0)
    engine.ctx = ctx
    engine.current = ctx.union_state()
    engine.lineage = LineageIndex(ctx)
    engine._revision = 0
    engine.reviewed_sources = {}
    engine.history = []
    old = engine.current
    with pytest.raises(IntegrationIncomplete):
        engine._run_task(LocalTask("delete", "reconcile", {r, e}), "unit")
    assert phases == ["reconcile", "graph_assess"] and engine.current == old


def test_completed_resume_feedback_stays_local_and_keeps_source_lineage(tmp_path):
    from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore

    source = state([*catalog(), concept("A", "a"), concept("B", "a")])
    schema = load_root_schema()
    first = IncrementalGraphIntegrationEngine(IdentityOnlyAgent())
    first.workspace_path = tmp_path / "first.sqlite"
    first.merge(state(), source, schema, run_id="first")
    snapshot = WorkspaceStore(first.workspace_path).load("first")
    target = next(n.id for n in first.current.graph.nodes if n.node_type == "concept")
    untouched = first.current
    agent = IdentityOnlyAgent()
    second = IncrementalGraphIntegrationEngine(agent)
    second.resume_snapshot = snapshot
    second.external_feedback = ["Review the definition without unrelated cleanup."]
    second.feedback_targets = [target]
    result = second.merge(state(), source, schema, run_id="local")
    assert agent.phases == ["reconcile"]
    assert second.current == untouched
    assert len(result.node_mappings) == len(source.graph.nodes)
    assert second.history[-1]["task_id"] == "external-review"

    import pytest

    snapshot["final"] = False
    with pytest.raises(ValueError, match="completed workspace"):
        second.merge(state(), source, schema, run_id="unfinished")


def test_local_reconciliation_does_not_reopen_unrelated_source_signals(monkeypatch):
    from cortex.capabilities.graph_merge.integration import incremental as module
    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.integration.lineage import LineageIndex

    original = state([*catalog(), concept("A", "a"), concept("B", "b")])
    ctx = IntegrationContext(state(), original, load_root_schema())
    engine = IncrementalGraphIntegrationEngine(None)
    engine.ctx, engine.current = ctx, ctx.union_state()
    engine.lineage = LineageIndex(ctx)
    a, b = ctx.input_node_map["A"], ctx.input_node_map["B"]
    calls = []
    monkeypatch.setattr(
        module,
        "inspection_signals",
        lambda *args, **kw: [{"id": "unrelated", "kind": "example", "node_ids": [b]}],
    )
    engine._run_task = lambda task, run_id: calls.append(task.targets) or set()
    engine._reconcile("local", initial_targets={a})
    assert calls == [{a}]


def test_later_identity_batch_does_not_reproject_a_consumed_member():
    import pytest

    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
    from cortex.capabilities.graph_merge.integration.incremental import (
        active_identity_projection,
    )
    from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
    from cortex.capabilities.graph_merge.integration.models import Change

    ctx = IntegrationContext(
        state(),
        state([*catalog(), concept("A", "a"), concept("B", "a")]),
        load_root_schema(),
    )
    before = ctx.union_state()
    a, b = ctx.input_node_map["A"], ctx.input_node_map["B"]
    after = before.model_copy(
        update={
            "graph": before.graph.model_copy(
                update={"nodes": tuple(n for n in before.graph.nodes if n.id != b)}
            )
        }
    )
    ledger = LineageIndex(ctx)
    with pytest.raises(IntegrationIncomplete, match="absent without"):
        active_identity_projection(after, {a: a, b: a}, ledger)
    ledger.observe(
        before,
        after,
        [
            Change.model_validate(
                {
                    "before": [{"kind": "node", "id": b}],
                    "after": [{"kind": "node", "id": a}],
                    "explanation": "Earlier coupled repair already incorporated this matched member.",
                }
            )
        ],
    )
    assert active_identity_projection(after, {a: a, b: a}, ledger) == {a: a}
    assert any(r.id == b for r in ledger.origin_refs("node", a))


def test_resume_reuses_exact_accepted_relation_coverage_without_model_calls(tmp_path):
    from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore

    original = state([*catalog(), concept("A", "a"), concept("B", "a")])
    first = IncrementalGraphIntegrationEngine(IdentityOnlyAgent())
    first.workspace_path = tmp_path / "first.sqlite"
    result = first.merge(state(), original, load_root_schema(), run_id="first")
    saved = WorkspaceStore(first.workspace_path).load("first")
    assert saved["relation_coverage"]
    agent = IdentityOnlyAgent()
    resumed = IncrementalGraphIntegrationEngine(agent)
    resumed.resume_snapshot = saved
    again = resumed.merge(state(), original, load_root_schema(), run_id="second")
    assert again.state == result.state
    assert agent.phases == []
