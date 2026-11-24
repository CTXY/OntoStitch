from types import SimpleNamespace

import pytest
from graph_merge_fixtures import catalog, concept, edge, state

from cortex.application.ontology_graph import GraphRejected
from cortex.capabilities.graph_merge.engine import IncrementalGraphIntegrationEngine
from cortex.capabilities.graph_merge.graph.repair import validation_targets
from cortex.schema import load_root_schema


def engine_with_contested_binding():
    engine = IncrementalGraphIntegrationEngine(None, max_rounds=5)
    engine.ctx = SimpleNamespace(schema=load_root_schema())
    valid = state([*catalog(), concept("A", "a"), concept("B", "a")])
    engine.current = valid.model_copy(update={"graph": valid.graph.model_copy(update={
        "edges": (*valid.graph.edges, edge("ground.A", "A", "a"), edge("ground.B", "B", "a"))
    })})
    engine._revision = 7
    return engine


def test_final_contract_error_opens_only_its_complete_binding_contest():
    engine = engine_with_contested_binding()
    with pytest.raises(GraphRejected) as captured:
        engine._publish_checks()
    targets = validation_targets(engine.current, captured.value.issues)
    assert {"A", "B", "a", "ground.A", "ground.B"} <= targets
    assert not {"b", "c", "table"} & targets
    tasks = []

    def repair(task, run_id):
        tasks.append(task)
        assert task.feedback[0]["code"] == "DUPLICATE_CONCEPT_GROUNDING"
        engine.current = engine.current.model_copy(update={"graph": engine.current.graph.model_copy(update={
            "edges": tuple(e for e in engine.current.graph.edges if e.id != "ground.B")
        })})

    engine._run_task = repair
    engine._validate_for_publication("unit")
    assert len(tasks) == 1
    assert tasks[0].targets == targets
    assert engine.current.graph.nodes == engine_with_contested_binding().current.graph.nodes


def test_noop_contract_repair_stops_without_repeating_model_tasks():
    engine = engine_with_contested_binding()
    tasks = []
    engine._run_task = lambda task, run_id: tasks.append(task)
    with pytest.raises(GraphRejected):
        engine._validate_for_publication("unit")
    assert len(tasks) == 1


def test_grounding_deletion_assessment_shows_the_remaining_canonical_owner():
    from cortex.capabilities.graph_merge.reconciliation.context import relation_context
    from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
    engine = engine_with_contested_binding()
    before = engine.current
    after = before.model_copy(update={"graph": before.graph.model_copy(update={
        "edges": tuple(e for e in before.graph.edges if e.id != "ground.B")
    })})
    ctx = SimpleNamespace(schema=load_root_schema(), evidence=[])
    packet = relation_context(ctx, after, LocalTask("repair", "graph_assess", {"B"}),
                              before=before, assessment=True)
    assert "ground.A" in {e["id"] for e in packet["edges"]}
    assert {"A", "B", "a"} <= {n["id"] for n in packet["nodes"]}
