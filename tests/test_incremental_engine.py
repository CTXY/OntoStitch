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


def test_no_edit_decision_judgment_still_requires_independent_content_review():
    import pytest
    from test_graph_integration import edge

    from cortex.capabilities.graph_merge.integration.context import IntegrationContext
    from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
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
    with pytest.raises(IntegrationIncomplete):
        engine._run_task(
            LocalTask("decision", "reconcile", set(ids), checks=[signal]), "unit"
        )
    assert phases == ["reconcile", "node_assess"]
    assert engine.current == before and not engine.history


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
            assert payload["reviewed_deleted_ids"] == [r]
            assert any(n["id"] == r for n in payload["deleted_nodes"])
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
    assert phases == ["reconcile", "node_assess"] and engine.current == old
