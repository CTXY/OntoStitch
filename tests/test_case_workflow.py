import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_rule, seed_accepted

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

    def accept_all(model, **payload):
        return assessment(
            payload["groups"],
            [key for g in payload["groups"] for key in g.member_ids],
            payload["targets"], validation=payload["validation"],
        )

    kwargs = {"assessor": assessor or accept_all}
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


def assessment(groups, accept, targets=None, validation=None):
    from assessment_fixtures import assessment_data
    return AssessmentFeedback.model_validate(assessment_data(groups, accept, targets or [], validation))


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
    assert result["assessment_accepted_asset_ids"] == [make_rule().id]
    assert result["accepted_revision"] == result["final_revision"] == state.accepted_revision
    assert result["case_status"] == "passed"
    assert result["validation_status"] == "stable_correct"
    # Viewing the output never creates an additional human approval gate.
    for field in ("review_only", "formal_case_acceptance", "proposal_revision"):
        assert field not in result
        assert field not in state.model_dump()
    assert json.loads(Path(result["result_path"]).read_text())["correct"]


@pytest.mark.parametrize("selected_sql_correct", [False, True])
@pytest.mark.parametrize("selected_assessment_ok", [False, True])
def test_filtered_candidate_needs_matching_sql_and_new_assessment(
    case_graph, selected_sql_correct, selected_assessment_ok
):
    reviews = []

    def sql_for(graph):
        ids = {n.id for n in graph.nodes if n.node_type == "generation_rule"}
        ok = len(ids) == 2 or selected_sql_correct and "generation_rule.good" in ids
        return "SELECT COUNT(*) FROM orders" if ok else "SELECT 0"

    def assessor(model, **payload):
        reviews.append(payload)
        assert not payload["accepted"].semantic_assets
        accept = (
            ["generation_rule.good"]
            if len(reviews) == 1 or selected_assessment_ok
            else []
        )
        return assessment(payload["groups"], accept, payload["targets"], payload["validation"])

    workflow, services, validator = make_workflow(
        case_graph,
        EditingAgent([[make_rule("good"), make_rule("bad")]]),
        Validator(sql_for),
        assessor,
        iterations=1,
    )
    # Direct filtering reaches fresh validation and assessment before acceptance.
    first = workflow.run(interrupt_after=["select"])
    assert not workflow.runtime.graph.accepted.semantic_assets
    assert len(workflow.runtime.graph.working.semantic_assets) == 1
    assert first.trial_revision != first.working_revision
    assert first.latest_feedback["filtered_from_assessment_ref"] == first.assessment_ref
    # Prevent another Learning attempt; test filtering verification even at cap.
    workflow.runtime.agent = EditingAgent([[]])
    state = workflow.run(resume=True)
    assert len(reviews) == 2
    assert reviews[0]["trial"].revision != reviews[1]["trial"].revision
    assert reviews[0]["validation"] != reviews[1]["validation"]
    accepted = selected_sql_correct and selected_assessment_ok
    assert bool(workflow.runtime.graph.accepted.semantic_assets) is accepted
    proof = services.evidence.read(state.acceptance_refs[0])["payload"]
    assert bool(proof["accepted_member_ids"]) is accepted
    assert proof["sql_stable_correct"] is selected_sql_correct
    assert proof["assessment_acceptable"] is selected_assessment_ok
    if accepted:
        assert state.completion_reason == "stable_correct"
        assert validator.generate_calls == 6


def test_no_changes_ends_inner_without_free_repeated_episode(case_graph):
    ctrl, _services, validator = make_workflow(
        case_graph,
        EditingAgent([[make_rule()], []]),
        Validator(lambda graph: "SELECT 0"),
    )
    state = ctrl.run()
    assert ctrl.runtime.agent.calls == 2
    assert state.iteration == 0
    assert state.completion_reason == "round_limit_reached"
    assert (
        validator.generate_calls == 2
    )  # tentative and accepted physical snapshots differ


def test_no_edit_on_failed_sql_rediagnoses_same_candidate_within_outer_budget(case_graph):
    workflow, services, _validator = make_workflow(
        case_graph,
        EditingAgent([[make_rule(action="first")], [], [make_rule(action="fixed")]]),
        Validator(lambda graph: "SELECT COUNT(*) FROM orders" if any(
            n.node_type == "generation_rule" and n.properties["action"] == "fixed"
            for n in graph.nodes
        ) else "SELECT 0"),
        rounds=2,
    )
    diagnosed = []
    original = services.diagnose

    def diagnose(snapshot, ref):
        diagnosed.append([a.properties.action for a in snapshot.semantic_assets])
        assert snapshot.revision == services.evidence.read(ref)["payload"]["graph_revision"]
        return original(snapshot, ref)

    services.diagnose = diagnose
    result = workflow.run()
    assert result.completion_reason == "stable_correct"
    assert result.round_number == 2
    assert diagnosed == [["first"], ["first"]]
    assert workflow.runtime.graph.accepted.semantic_assets[0].properties.action == "fixed"


def test_safe_checkpoint_resume_keeps_acceptance_and_reuses_gate(case_graph):
    agent = EditingAgent([[make_rule()]])
    ctrl, _services, validator = make_workflow(case_graph, agent)
    ctrl.run(interrupt_after=["direct"])  # persisted Direct boundary
    accepted = ctrl.runtime.graph.accepted_revision
    assert not ctrl.runtime.graph.accepted.semantic_assets
    ctrl.run(resume=True, interrupt_after=["validate_initial"])
    assert ctrl.runtime.graph.accepted_revision == accepted
    assert not ctrl.runtime.graph.accepted.semantic_assets
    resumed, services2, _ = make_workflow(case_graph, EditingAgent([]), validator)
    state = resumed.run(resume=True)
    assert state.accepted_revision != accepted
    assert len(state.acceptance_refs) == 1
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
    seed_accepted(ctrl.runtime.graph)
    result = case_result(
        ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
    )
    assert not result["correct"]
    assert result["validation_status"] == "unavailable"
    assert result["asset_outcome"] == "partial"


def test_assessment_failure_cannot_accept_direct_or_repair_assets(case_graph):
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
    assert not ctrl.runtime.graph.accepted.semantic_assets
    assert (
        case_result(
            ctrl.runtime.record, state, ctrl.runtime.graph, services, ctrl.root
        )["asset_outcome"]
        == "empty"
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
    ctrl.run(interrupt_after=["accept"])
    write_json_atomic(
        ctrl.root / "case.json",
        {"source": ctrl.runtime.record, "run_id": "resumed", "case_index": 1},
    )
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
    write_json_atomic(
        ctrl.root / "case.json",
        {"source": ctrl.runtime.record, "run_id": "refused", "case_index": 1},
    )
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


def test_failed_sql_never_accepts_and_new_diagnosis_uses_failed_candidate(case_graph):
    baselines = []

    def assessor(model, **payload):
        baselines.append(payload["baseline"].semantic_assets)
        return assessment(payload["groups"], [make_rule().id], payload["targets"], payload["validation"])

    workflow, services, validator = make_workflow(
        case_graph,
        EditingAgent(
            [
                [make_rule(action="m1")],
                [make_rule(action="m2")],
                [make_rule(action="m3")],
            ]
        ),
        Validator(
            lambda graph: (
                "SELECT COUNT(*) FROM orders"
                if any(
                    n.node_type == "generation_rule" and n.properties["action"] == "m3"
                    for n in graph.nodes
                )
                else "SELECT 0"
            )
        ),
        assessor,
        rounds=2,
        iterations=1,
    )
    diagnosed = []
    original = services.diagnose

    def diagnose(snapshot, ref):
        assert (
            services.evidence.read(ref)["payload"]["content_fingerprint"]
            == snapshot.content_fingerprint
        )
        diagnosed.append([a.properties.action for a in snapshot.semantic_assets])
        return original(snapshot, ref)

    services.diagnose = diagnose
    state = workflow.run()
    assert state.completion_reason == "stable_correct"
    assert state.round_number == 2 and state.iteration == 1
    assert baselines == [[], []]
    assert diagnosed == [["m1"], ["m2"]]
    proofs = [services.evidence.read(ref)["payload"] for ref in state.acceptance_refs]
    assert proofs[0]["accepted_member_ids"] == []
    assert not proofs[0]["sql_stable_correct"]
    assert proofs[1]["accepted_member_ids"] == [make_rule().id]
    assert validator.generate_calls == 5  # No empty accepted-graph replay between diagnoses.


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
    assert result["completion_reason"] == "resource_limit"
    assert result["correct"] is False
    assert result["case_status"] == "needs_review"
    assert result["draft_node_count"] == 0
    assert "draft_nodes" not in result
    assert result["asset_outcome"] == "empty"
    assert result["sql_correct"]  # SQL success does not complete the extraction episode.


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
    assert not workflow.runtime.graph.accepted.semantic_assets
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
        lambda model, **payload: assessment(payload["groups"], [make_rule().id], payload["targets"], payload["validation"]),
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
    write_json_atomic(
        workflow.root / "case.json",
        {"source": workflow.runtime.record, "run_id": "resumed", "case_index": 1},
    )
    ontology = workflow.root / "result" / "ontology.json"
    if damage == "missing":
        ontology.unlink()
    else:
        ontology.write_text("[]")
    pipeline = TrainPipeline(
        services.config,
        model=ScriptModel([]),
        database=workflow.runtime.database,
        validation_client=validator,
    )
    result = pipeline.resume_case(workflow.root)
    assert not result["correct"]
    assert result["case_status"] == "failed"
    assert result["result_path"] is None
    assert result["ontology_path"] is None
    assert workflow.inspect().values["accepted_revision"] == state.accepted_revision


@pytest.mark.parametrize("verdict", ["invalid", "inconclusive"])
def test_correct_trial_with_unaccepted_group_reuses_assessment_in_next_episode(
    case_graph, verdict
):
    class FeedbackAgent(EditingAgent):
        def run(self, session):
            if self.calls == 1:
                assert session.feedback["unit_results"][0]["quality"] == verdict
                assert (
                    session.graph.working.semantic_assets[0].properties.action
                    == "draft"
                )
                assert not session.graph.accepted.semantic_assets
            return super().run(session)

    reviews = []

    def assessor(model, **payload):
        reviews.append(payload)
        from assessment_fixtures import assessment_data
        statuses = {m: verdict for g in payload["groups"] for m in g.member_ids} if len(reviews) == 1 else None
        return AssessmentFeedback.model_validate(assessment_data(payload["groups"], targets=payload["targets"], validation=payload["validation"], quality_by_id=statuses))


    workflow, _, _ = make_workflow(
        case_graph,
        FeedbackAgent([[make_rule(action="draft")], [make_rule(action="revised")]]),
        Validator(
            lambda graph: (
                "SELECT COUNT(*) FROM orders"
                if any(n.node_type == "generation_rule" for n in graph.nodes)
                else "SELECT 0"
            )
        ),
        assessor,
        rounds=1,
        iterations=2,
    )
    workflow.skip_direct = True
    state = workflow.run()
    assert state.completion_reason == "stable_correct"
    assert state.round_number == 1 and state.iteration == 2
    assert workflow.runtime.agent.calls == 2 and len(reviews) == 2
    assert (
        workflow.runtime.graph.accepted.semantic_assets[0].properties.action
        == "revised"
    )


@pytest.mark.parametrize("skip_direct", [False, True])
@pytest.mark.parametrize("verdict", ["invalid", "inconclusive"])
def test_direct_and_repair_require_assessment_even_when_sql_stable(
    case_graph, skip_direct, verdict
):
    calls = []

    class ReviewedAgent(EditingAgent):
        def run(self, session):
            if self.calls == 1:
                assert session.feedback["unit_results"][0]["quality"] == verdict
                assert not session.graph.accepted.semantic_assets
            return super().run(session)

    def assessor(model, **payload):
        calls.append(payload)
        feedback = assessment(payload["groups"], [make_rule().id], payload["targets"], payload["validation"])
        if len(calls) == 1:
            from assessment_fixtures import assessment_data
            feedback = AssessmentFeedback.model_validate(assessment_data(payload["groups"], targets=payload["targets"], validation=payload["validation"], quality_by_id={m:verdict for g in payload["groups"] for m in g.member_ids}))
        return feedback

    workflow, services, _ = make_workflow(
        case_graph,
        ReviewedAgent([[make_rule(action="draft")], [make_rule(action="fixed")]]),
        Validator(
            lambda graph: (
                "SELECT COUNT(*) FROM orders"
                if any(n.node_type == "generation_rule" for n in graph.nodes)
                else "SELECT 0"
            )
        ),
        assessor,
        iterations=2,
    )
    workflow.skip_direct = skip_direct
    state = workflow.run()
    assert state.completion_reason == "stable_correct"
    assert len(calls) == 2
    assert (
        workflow.runtime.graph.accepted.semantic_assets[0].properties.action == "fixed"
    )
    proofs = [services.evidence.read(ref)["payload"] for ref in state.acceptance_refs]
    assert proofs[0]["accepted_revision_before"] == proofs[0]["accepted_revision_after"]
    assert proofs[0]["accepted_member_ids"] == []
    assert not proofs[0]["assessment_acceptable"]
    assert proofs[1]["sql_stable_correct"] and proofs[1]["assessment_acceptable"]


def test_repair_limit_cannot_accept_assessment_approved_but_failed_sql(case_graph):
    workflow, services, _ = make_workflow(
        case_graph,
        EditingAgent([[make_rule()]]),
        Validator(lambda graph: "SELECT 0"),
        rounds=1,
        iterations=1,
    )
    workflow.skip_direct = True
    state = workflow.run()
    assert state.completion_reason == "round_limit_reached"
    assert not workflow.runtime.graph.accepted.semantic_assets
    assert not workflow.runtime.graph.working.semantic_assets
    proof = services.evidence.read(state.acceptance_refs[0])["payload"]
    assert proof["assessment_acceptable"] and not proof["sql_stable_correct"]
    assert proof["accepted_member_ids"] == []
    assert proof["accepted_revision_before"] == proof["accepted_revision_after"]


@pytest.mark.parametrize("damage", ["sql", "assessment"])
def test_accept_refuses_evidence_for_another_candidate(case_graph, damage):
    workflow, services, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = workflow.run(interrupt_after=["select"])
    before = workflow.runtime.graph.accepted_revision
    if damage == "sql":
        # Valid evidence from the same case/context, but for the physical graph.
        state.validation_ref = services.validate(workflow.runtime.graph.accepted)
    else:
        saved = services.evidence.read(state.assessment_ref)["payload"]
        state.assessment_ref = services.evidence.put(
            "assessment", {**saved, "trial_revision": before}
        )
    with pytest.raises(ValueError, match="assessment does not match"):
        workflow.runtime.accept(state)
    assert workflow.runtime.graph.accepted_revision == before


def test_failed_filter_preserves_projection_error_for_learning(case_graph, monkeypatch):
    workflow, _, _ = make_workflow(
        case_graph,
        EditingAgent(
            [
                [
                    make_rule("dependency"),
                    make_rule("dependent"),
                ]
            ]
        ),
        assessor=lambda model, **p: assessment(
            p["groups"], ["generation_rule.dependent"], p["targets"], p["validation"]
        ),
    )

    def invalid_projection(**kwargs):
        raise ValueError("accepted group depends on unaccepted tentative content")

    monkeypatch.setattr(workflow.runtime.graph, "select", invalid_projection)
    state = workflow.run(interrupt_after=["accept"])
    assert not workflow.runtime.graph.accepted.semantic_assets
    assert "unaccepted tentative content" in state.latest_feedback["selection_error"]
    assert state.latest_feedback["unit_results"]
