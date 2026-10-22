import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_rule

from cortex.capabilities.sql_training.artifacts.cases import case_result, seal_case
from cortex.capabilities.sql_training.graph.mutations import (
    ChangeGroup,
    GraphEdit,
    apply_edit,
)
from cortex.capabilities.sql_training.learning.session import EpisodeResult
from cortex.capabilities.sql_training.runtime_config import (
    RuntimeDatabaseConfig,
    RuntimeValidationConfig,
    SqlTrainingRuntimeConfig,
)
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.capabilities.sql_training.training.feedback import AssessmentFeedback
from cortex.capabilities.sql_training.training.services import CaseServices
from cortex.capabilities.sql_training.training.workflow import CaseWorkflow
from cortex.capabilities.sql_training.validation.models import (
    GenerationResponse,
    GenerationResult,
)
from cortex.config.models import SqlTrainingConfig


class Validator:
    def __init__(self, sql_for_graph=lambda graph: "SELECT COUNT(*) FROM orders"):
        self.sql_for_graph = sql_for_graph
        self.generate_calls = 0

    def retrieve(self, *, graph, **kwargs):
        return SimpleNamespace(
            status="completed",
            subgraph=graph,
            paths=(),
            selected_node_ids=tuple(
                n.id for n in graph.nodes if n.node_type == "generation_rule"
            ),
            raw_retrieved_node_ids=tuple(n.id for n in graph.nodes),
        )

    def generate(self, request):
        self.generate_calls += 1
        return GenerationResponse(
            code=200,
            data=[GenerationResult(query=self.sql_for_graph(request.graph_space))],
        )


class EditingAgent:
    """Deterministic decision substitute, using the real domain editing boundary."""

    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    def run(self, session):
        self.calls += 1
        start = session.graph.working_revision
        changes = self.actions.pop(0)
        if changes:
            existing = {a.id for a in session.graph.working.semantic_assets}
            edit = GraphEdit.model_validate(
                {
                    "edit_id": f"episode{self.calls}",
                    "expected_revision": start,
                    "operations": [
                        {
                            "operation": "update" if a.id in existing else "create",
                            "node_id": a.id,
                            "asset": a.model_dump(mode="json"),
                        }
                        for a in changes
                    ],
                }
            )
            apply_edit(
                session.graph,
                edit,
                database=session.database,
                source_id=session.record["source_id"],
            )
        groups = [
            ChangeGroup(group_id=a.id, member_ids=[a.id], target_ids=[a.id])
            for a in changes
        ]
        return EpisodeResult(
            outcome="ready_for_validation",
            starting_revision=start,
            delivered_revision=session.graph.working_revision,
            direct_completed=session.mode == "direct",
            direct_type_coverage=["concept", "metric", "generation_rule"],
            proposed_groups=groups,
        )


def make_workflow(
    case_graph, agent, validator=None, assessor=None, rounds=1, iterations=2
):
    store, database, workspace = case_graph
    config = SqlTrainingRuntimeConfig(
        workspace=str(workspace.root),
        database=RuntimeDatabaseConfig(url=database.url, dialect="sqlite"),
        validation=RuntimeValidationConfig(dialect="sqlite"),
        train=SqlTrainingConfig(max_rounds=rounds, max_iterations=iterations),
    )
    record = {
        "source_id": "case",
        "question": "How many orders?",
        "sql": "SELECT COUNT(*) FROM orders",
    }
    evidence = EvidenceStore(store.root.parent / "evidence")
    validator = validator or Validator()
    services = CaseServices(
        config=config,
        model=SimpleNamespace(),
        validation_client=validator,
        graph=store,
        evidence=evidence,
        record=record,
    )

    def diagnose(snapshot, validation):
        targets = [{"target_id": "target", "instruction": "fix SQL"}]
        return targets, evidence.put("diagnosis", {"targets": targets}), "completed"

    services.diagnose = diagnose
    kwargs = {"assessor": assessor} if assessor else {}
    ctrl = CaseWorkflow(
        root=store.root.parent,
        graph=store,
        evidence=evidence,
        services=services,
        agent=agent,
        database=database,
        record=record,
        max_rounds=rounds,
        max_iterations=iterations,
        **kwargs,
    )
    return ctrl, services, validator


def assessment(groups, accept):
    return AssessmentFeedback.model_validate(
        {
            "group_results": [
                {
                    "group_id": g.group_id,
                    "verdict": "acceptable"
                    if set(g.member_ids) <= set(accept)
                    else "reject",
                    "evidence": "source and SQL",
                }
                for g in groups
            ],
            "target_results": [{"target_id": "target", "status": "unresolved"}],
        }
    )


def test_direct_success_one_gate_and_semantic_only_bundle(case_graph):
    ctrl, services, validator = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = ctrl.run()
    assert state.completion_reason == "stable_correct"
    assert validator.generate_calls == 3  # no extra final gate
    result = seal_case(
        ctrl.root,
        case_result(
            ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
        ),
        ctrl.runtime.graph,
    )
    assert result["correct"] and result["asset_outcome"] == "ready"
    assert result["draft_node_ids"] == [make_rule().id]
    assert result["directly_retained_asset_ids"] == [make_rule().id]
    assert result["assessment_accepted_asset_ids"] == []
    assert json.loads(Path(result["result_path"]).read_text())[
        "correct"
    ]


@pytest.mark.parametrize("final_correct", [False, True])
def test_partial_acceptance_revalidates_retained_m2_not_trial_m3(
    case_graph, final_correct
):
    def sql_for(graph):
        actions = {
            n.id: n.properties["action"]
            for n in graph.nodes
            if n.node_type == "generation_rule"
        }
        m, c = actions.get("generation_rule.m"), actions.get("generation_rule.c")
        correct = c == "c2" and (m == "m3" or final_correct and m == "m2")
        return "SELECT COUNT(*) FROM orders" if correct else "SELECT 0"

    calls = []

    def assessor(model, **payload):
        calls.append(payload)
        accept = ["generation_rule.m"] if len(calls) == 1 else ["generation_rule.c"]
        return assessment(payload["groups"], accept)

    agent = EditingAgent(
        [
            [make_rule("m", "m1"), make_rule("c", "c1")],
            [make_rule("m", "m2")],
            [make_rule("m", "m3"), make_rule("c", "c2")],
        ]
    )
    ctrl, services, validator = make_workflow(
        case_graph, agent, Validator(sql_for), assessor
    )
    state = ctrl.run()
    actions = {
        a.id: a.properties.action for a in ctrl.runtime.graph.accepted.semantic_assets
    }
    assert actions == {"generation_rule.m": "m2", "generation_rule.c": "c2"}
    assert state.round_number == 1 and state.iteration == 2
    assert state.completion_reason == (
        "stable_correct" if final_correct else "round_limit_reached"
    )
    result = case_result(
        ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
    )
    assert result["correct"] is final_correct
    assert result["asset_outcome"] == ("ready" if final_correct else "partial")
    assert validator.generate_calls == (8 if final_correct else 6)
    assert calls[1]["accepted"].semantic_assets[1].properties.action in {"m2", "c1"}


def test_no_changes_ends_inner_without_free_repeated_episode(case_graph):
    ctrl, _services, validator = make_workflow(
        case_graph,
        EditingAgent([[make_rule()], []]),
        Validator(lambda graph: "SELECT 0"),
    )
    state = ctrl.run()
    assert ctrl.runtime.agent.calls == 2
    assert state.iteration == 0
    assert state.completion_reason == "no_progress"
    assert validator.generate_calls == 1  # cached failed gate at finalization


def test_safe_checkpoint_resume_keeps_acceptance_and_reuses_gate(case_graph):
    agent = EditingAgent([[make_rule()]])
    ctrl, _services, validator = make_workflow(case_graph, agent)
    ctrl.run(interrupt_after=["direct"])  # persisted Direct boundary
    accepted = ctrl.runtime.graph.accepted_revision
    ctrl.run(resume=True, interrupt_after=["validate_initial"])
    resumed, services2, _ = make_workflow(case_graph, EditingAgent([]), validator)
    state = resumed.run(resume=True)
    assert state.accepted_revision == accepted
    assert services2.replay(state.validation_ref).correct
    assert validator.generate_calls == 3
    assert resumed.runtime.agent.calls == 0


def test_evidence_mismatch_cannot_export_success(case_graph):
    ctrl, services, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = ctrl.run()
    from cortex.capabilities.sql_training.graph.mutations import GraphEdit, apply_edit

    request = GraphEdit.model_validate(
        {
            "edit_id": "later",
            "expected_revision": ctrl.runtime.graph.working_revision,
            "operations": [
                {
                    "operation": "create",
                    "node_id": "generation_rule.other",
                    "asset": make_rule("other").model_dump(mode="json"),
                }
            ],
        }
    )
    apply_edit(
        ctrl.runtime.graph, request, database=ctrl.runtime.database, source_id="case"
    )
    ctrl.runtime.graph.retain_direct()
    result = case_result(
        ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
    )
    assert not result["correct"]
    assert result["validation_status"] == "unavailable"
    assert result["asset_outcome"] == "partial"


def test_assessment_failure_preserves_initial_accepted_assets(case_graph):
    def broken(*args, **kwargs):
        raise ValueError("invalid assessment")

    ctrl, services, _ = make_workflow(
        case_graph,
        EditingAgent([[make_rule(action="m1")], [make_rule(action="m2")]]),
        Validator(lambda graph: "SELECT 0"),
        broken,
    )
    with pytest.raises(ValueError, match="invalid assessment"):
        ctrl.run()
    state = ctrl.state.model_copy(update={"operational_failure": True})
    assert ctrl.runtime.graph.accepted.semantic_assets[0].properties.action == "m1"
    assert (
        case_result(
            ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
        )["asset_outcome"]
        == "partial"
    )


def test_infrastructure_retries_are_bounded_and_do_not_resample_semantic_failure(
    case_graph,
):
    class Transient(Validator):
        def __init__(self):
            super().__init__()
            self.retrieval_calls = 0

        def retrieve(self, **kwargs):
            self.retrieval_calls += 1
            if self.retrieval_calls == 1:
                raise ConnectionError("temporary outage")
            return super().retrieve(**kwargs)

    validator = Transient()
    ctrl, _services, _ = make_workflow(
        case_graph, EditingAgent([[make_rule()]]), validator
    )
    assert ctrl.run().completion_reason == "stable_correct"
    assert validator.retrieval_calls == 4
    assert validator.generate_calls == 3


def test_inconclusive_diagnosis_has_distinct_stop_reason(case_graph):
    ctrl, services, _ = make_workflow(
        case_graph, EditingAgent([[make_rule()]]), Validator(lambda graph: "SELECT 0")
    )
    services.diagnose = lambda *args: (
        [],
        services.evidence.put("diagnosis", {}),
        "analysis_incomplete",
    )
    state = ctrl.run()
    assert state.completion_reason == "diagnosis_inconclusive"
    assert not state.operational_failure


def test_public_resume_api_reconstructs_graph_and_finishes_without_repeating_direct(
    case_graph,
):
    from test_agent_runtime import ScriptModel

    from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
    from cortex.capabilities.sql_training.pipeline.train import TrainPipeline

    ctrl, services, validator = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    ctrl.run(interrupt_after=["direct"])
    write_json_atomic(ctrl.root / "case.json", {
        "source": ctrl.runtime.record, "run_id": "resumed", "case_index": 1
    })
    model = ScriptModel([])
    pipeline = TrainPipeline(
        services.config,
        model=model,
        database=ctrl.runtime.database,
        validation_client=validator,
    )
    result = pipeline.resume_case(ctrl.root)
    assert result["correct"], result
    assert result["directly_retained_asset_ids"] == [make_rule().id]
    assert len(model._calls) == 0


def test_resume_context_mismatch_refuses_without_mutating_checkpoint(case_graph):
    from test_agent_runtime import ScriptModel

    from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
    from cortex.capabilities.sql_training.pipeline.train import TrainPipeline

    ctrl, services, validator = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    ctrl.run(interrupt_after=["direct"])
    write_json_atomic(ctrl.root / "case.json", {
        "source": ctrl.runtime.record, "run_id": "refused", "case_index": 1
    })
    original = ctrl.inspect().values
    changed = services.config.model_copy(
        update={
            "train": services.config.train.model_copy(update={"agent_max_steps": 11})
        }
    )
    pipeline = TrainPipeline(
        changed,
        model=ScriptModel([]),
        database=ctrl.runtime.database,
        validation_client=validator,
    )
    with pytest.raises(ValueError, match="changed source/data/config/prompt"):
        pipeline.resume_case(ctrl.root)
    assert ctrl.inspect().values == original
    assert not (ctrl.root / "progress.json").exists()


def test_second_round_starts_from_latest_accepted_graph_and_matching_failure(
    case_graph,
):
    seen = []

    def sql_for(graph):
        actions = [
            n.properties["action"]
            for n in graph.nodes
            if n.node_type == "generation_rule"
        ]
        return "SELECT COUNT(*) FROM orders" if "m3" in actions else "SELECT 0"

    def assessor(model, **payload):
        seen.append(payload["baseline"].semantic_assets[0].properties.action)
        return assessment(payload["groups"], [make_rule().id])

    ctrl, services, validator = make_workflow(
        case_graph,
        EditingAgent(
            [
                [make_rule(action="m1")],
                [make_rule(action="m2")],
                [make_rule(action="m3")],
            ]
        ),
        Validator(sql_for),
        assessor,
        rounds=2,
        iterations=1,
    )
    original = services.diagnose
    diagnosed = []

    def diagnose(snapshot, ref):
        diagnosed.append(
            (
                snapshot.semantic_assets[0].properties.action,
                services.replay(ref).terminal_evaluation["query"],
            )
        )
        return original(snapshot, ref)

    services.diagnose = diagnose
    state = ctrl.run()
    assert state.completion_reason == "stable_correct"
    assert state.round_number == 2 and state.iteration == 1
    assert seen == ["m1", "m2"]
    assert diagnosed == [("m1", "SELECT 0"), ("m2", "SELECT 0")]
    assert validator.generate_calls == 5


def test_incomplete_direct_does_not_promote_legal_partial_semantics(case_graph):
    class Incomplete(EditingAgent):
        def run(self, session):
            result = super().run(session)
            return result.model_copy(
                update={"direct_completed": False, "outcome": "resource_limit"}
            )

    ctrl, services, _ = make_workflow(case_graph, Incomplete([[make_rule()]]))
    state = ctrl.run()
    result = case_result(
        ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
    )
    assert not result["direct_completed"]
    assert result["draft_node_count"] == 0
    assert "draft_nodes" not in result
    assert result["asset_outcome"] == "empty"
    assert result[
        "correct"
    ]  # base physical graph independently passes; partial semantic content is not exported


@pytest.mark.parametrize("empty_at_trial", [False, True])
def test_empty_generated_sql_finishes_without_assessment_or_infrastructure_resume(
    case_graph, empty_at_trial
):
    def sql_for(graph):
        actions = [
            n.properties["action"]
            for n in graph.nodes
            if n.node_type == "generation_rule"
        ]
        if empty_at_trial and actions == ["m1"]:
            return "SELECT 0"
        return ""

    def must_not_assess(*args, **kwargs):
        raise AssertionError("cannot assess an empty generated query")

    actions = [[make_rule(action="m1")]]
    if empty_at_trial:
        actions.append([make_rule(action="m2")])
    workflow, _services, validator = make_workflow(
        case_graph, EditingAgent(actions), Validator(sql_for), must_not_assess
    )
    state = workflow.run()
    assert state.completion_reason == "candidate_generation_failed"
    assert not state.operational_failure
    assert not state.acceptance_refs
    assert workflow.runtime.graph.accepted.semantic_assets[0].properties.action == "m1"
    assert not workflow.inspect().next
    resumed, _, _ = make_workflow(case_graph, EditingAgent([]), validator)
    assert resumed.run(resume=True).result_ref == state.result_ref


def test_skip_direct_policy_uses_same_graph_and_repairs_from_physical_base(case_graph):
    def sql_for(graph):
        return (
            "SELECT COUNT(*) FROM orders"
            if any(n.node_type == "generation_rule" for n in graph.nodes)
            else "SELECT 0"
        )

    workflow, _, _ = make_workflow(
        case_graph,
        EditingAgent([[make_rule()]]),
        Validator(sql_for),
        lambda model, **payload: assessment(payload["groups"], [make_rule().id]),
        rounds=1,
        iterations=1,
    )
    workflow.skip_direct = True
    state = workflow.run()
    assert state.completion_reason == "stable_correct"
    assert state.round_number == 1 and state.iteration == 1
    assert state.directly_retained_ids == []
    assert len(state.acceptance_refs) == 1
    assert workflow.runtime.agent.calls == 1


@pytest.mark.parametrize("damage", ["missing", "tampered"])
@pytest.mark.parametrize("pending_seal", [False, True])
def test_resume_rejects_damaged_formal_ontology(
    case_graph, damage, pending_seal, monkeypatch
):
    from test_agent_runtime import ScriptModel

    from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
    from cortex.capabilities.sql_training.pipeline.train import TrainPipeline

    workflow, services, validator = make_workflow(
        case_graph, EditingAgent([[make_rule()]])
    )
    if pending_seal:
        from cortex.capabilities.sql_training.training.evidence import EvidenceStore

        completed = EvidenceStore.completed

        def crash_after_seal(self, node, inputs, action):
            output = completed(self, node, inputs, action)
            if node == "seal":
                raise RuntimeError("checkpoint interrupted after seal")
            return output

        with monkeypatch.context() as patch:
            patch.setattr(EvidenceStore, "completed", crash_after_seal)
            with pytest.raises(RuntimeError, match="checkpoint interrupted"):
                workflow.run()
        assert workflow.inspect().next == ("seal",)
        state = workflow.state
    else:
        state = workflow.run()
    write_json_atomic(workflow.root / "case.json", {
        "source": workflow.runtime.record, "run_id": "resumed", "case_index": 1
    })
    ontology = workflow.root / "result" / "ontology.json"
    if damage == "missing":
        ontology.unlink()
    else:
        ontology.write_text("[]")
    pipeline = TrainPipeline(
        services.config, model=ScriptModel([]),
        database=workflow.runtime.database, validation_client=validator,
    )
    result = pipeline.resume_case(workflow.root)
    assert not result["correct"]
    assert result["case_status"] == "failed"
    assert result["result_path"] is None
    assert result["ontology_path"] is None
    assert workflow.inspect().values["accepted_revision"] == state.accepted_revision
