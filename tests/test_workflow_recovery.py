"""Crash windows are exercised against real LangGraph SQLite checkpoints."""

import json
from pathlib import Path

import pytest
from conftest import make_rule
from test_case_workflow import EditingAgent, Validator, assessment, make_workflow

from cortex.capabilities.sql_training.graph.mutations import ChangeGroup
from cortex.capabilities.sql_training.graph.snapshots import GraphStore
from cortex.capabilities.sql_training.learning.session import EpisodeResult
from cortex.capabilities.sql_training.learning.tools import build_tools
from cortex.capabilities.sql_training.training.evidence import EvidenceStore
from cortex.capabilities.sql_training.training.workflow import CaseResumeError
from cortex.capabilities.sql_training.training.workflow.runtime import case_writer
from cortex.capabilities.sql_training.training.workflow.state import CaseState
from cortex.schema import load_root_schema


def fresh_case_graph(case_graph):
    """Reconstruct the repository instead of reusing any live W/L pointers."""
    store, database, workspace = case_graph
    return (
        GraphStore(store.root, workspace=workspace, schema=load_root_schema()),
        database,
        workspace,
    )


def correct_when_action(action):
    return Validator(
        lambda graph: (
            "SELECT COUNT(*) FROM orders"
            if any(
                n.node_type == "generation_rule" and n.properties["action"] == action
                for n in graph.nodes
            )
            else "SELECT 0"
        )
    )


def fail_after_completed_operation(monkeypatch, target):
    completed = EvidenceStore.completed
    captured = {}

    def crash(self, node, inputs, action):
        result = completed(self, node, inputs, action)
        if node == target and not captured:
            captured["result"] = CaseState.model_validate(result)
            raise RuntimeError(f"crash after {target} receipt before checkpoint")
        return result

    monkeypatch.setattr(EvidenceStore, "completed", crash)
    return captured


def test_accept_receipt_replay_advances_exact_same_version_without_reassessment(
    case_graph, monkeypatch
):
    calls = []

    def assessor(model, **payload):
        calls.append(payload["trial"].revision)
        return assessment(payload["groups"], [make_rule().id], payload["targets"], payload["validation"])

    validator = correct_when_action("m2")
    workflow, _services, _ = make_workflow(
        case_graph,
        EditingAgent([[make_rule(action="m1")], [make_rule(action="m2")]]),
        validator,
        assessor,
    )
    captured = fail_after_completed_operation(monkeypatch, "accept")
    with pytest.raises(RuntimeError, match="crash after accept"):
        workflow.run()
    committed = captured["result"]
    checkpoint = workflow.inspect()
    assert checkpoint.next == ("accept",)
    assert checkpoint.values["accepted_revision"] != committed.accepted_revision
    assert not workflow.runtime.graph.accepted.semantic_assets

    resumed, _services, _ = make_workflow(
        fresh_case_graph(case_graph), EditingAgent([]), validator, assessor
    )

    def must_not_promote_again(**kwargs):
        raise AssertionError("completed acceptance must replay its original result")

    monkeypatch.setattr(resumed.runtime, "accept", must_not_promote_again)
    result = resumed.run(resume=True)
    assert result.accepted_revision == committed.accepted_revision
    assert result.acceptance_refs == committed.acceptance_refs
    assert len(result.acceptance_refs) == 1
    assert resumed.runtime.graph.accepted.semantic_assets[0].properties.action == "m2"
    assert len(calls) == 1
    assert resumed.runtime.agent.calls == 0
    assert validator.generate_calls == 4  # one failed base + three correct Trial SQLs
    assert result.completion_reason == "stable_correct"
    assert not resumed.inspect().next


def test_incomplete_episode_restarts_from_checkpoint_with_fresh_session_memory(
    case_graph,
):
    class ToolEditingAgent:
        def __init__(self, *, fail_repair):
            self.fail_repair = fail_repair
            self.starts = []

        def run(self, session):
            self.starts.append(
                (
                    session.mode,
                    session.graph.working_revision,
                    session.notes.current_plan,
                )
            )
            if session.mode == "repair":
                assert (
                    session.graph.working.semantic_assets[0].properties.action == "m1"
                )
                assert session.notes.current_plan != "uncommitted plan"
            starting = session.graph.working_revision
            asset = make_rule(action="m1" if session.mode == "direct" else "m2")
            tool = next(
                tool for tool in build_tools(session) if tool.name == "edit_graph"
            )
            tool.invoke(
                {
                    "edit_id": "same-agent-edit-id",
                    "expected_revision": starting,
                    "upserts": [asset.model_dump(mode="json")],
                }
            )
            if session.mode == "repair" and self.fail_repair:
                session.notes.current_plan = "uncommitted plan"
                raise ConnectionError("episode lost after graph edit")
            return EpisodeResult(
                outcome="ready_for_validation",
                starting_revision=starting,
                delivered_revision=session.graph.working_revision,
                direct_completed=session.mode == "direct",
                direct_type_coverage=["concept", "metric", "generation_rule"],
                proposed_groups=[
                    ChangeGroup(
                        group_id="count", member_ids=[asset.id], target_ids=["target"]
                    )
                ],
            )

    def assessor(model, **payload):
        return assessment(payload["groups"], [make_rule().id], payload["targets"], payload["validation"])

    validator = correct_when_action("m2")
    first = ToolEditingAgent(fail_repair=True)
    workflow, _services, _ = make_workflow(case_graph, first, validator, assessor)
    with pytest.raises(ConnectionError, match="episode lost"):
        workflow.run()
    snapshot = workflow.inspect()
    assert snapshot.next == ("repair",)
    base = snapshot.values["working_revision"]
    assert workflow.runtime.graph.working_revision == base
    assert workflow.runtime.graph.working.semantic_assets[0].properties.action == "m1"
    assert not (workflow.root / "edits").exists()

    second = ToolEditingAgent(fail_repair=False)
    resumed, _services, _ = make_workflow(
        fresh_case_graph(case_graph), second, validator, assessor
    )
    state = resumed.run(resume=True)
    assert second.starts == [("repair", base, "")]
    assert state.completion_reason == "stable_correct"
    assert resumed.runtime.graph.accepted.semantic_assets[0].properties.action == "m2"
    assert not (workflow.root / "edits").exists()
    assert state.iteration == 1


def test_seal_receipt_replay_preserves_exact_result_and_closes_new_resources(
    case_graph, monkeypatch
):
    workflow, _services, validator = make_workflow(
        case_graph, EditingAgent([[make_rule()]])
    )
    captured = fail_after_completed_operation(monkeypatch, "seal")
    with pytest.raises(RuntimeError, match="crash after seal"):
        workflow.run()
    bundle = Path(captured["result"].result_ref)
    before = bundle.read_bytes()
    assert workflow.inspect().next == ("seal",)

    resumed, _services, _ = make_workflow(
        fresh_case_graph(case_graph), EditingAgent([]), validator
    )
    closed = []
    resumed.runtime.close_resources = lambda: closed.append("closed")
    state = resumed.run(resume=True)
    assert state.result_ref == str(bundle)
    assert bundle.read_bytes() == before
    metadata = json.loads(before)
    assert metadata["correct"]
    assert metadata["result_path"] == str(bundle)
    assert Path(metadata["ontology_path"]) == bundle.parent / "ontology.json"
    assert "draft_nodes" not in metadata
    assert {path.name for path in bundle.parent.iterdir()} == {
        "result.json",
        "ontology.json",
    }
    assert closed == ["closed"]
    assert resumed.runtime.agent.calls == 0
    assert validator.generate_calls == 3


def test_deterministic_completed_node_evidence_is_available_to_read_tool(case_graph):
    from test_agent_runtime import session_for

    workflow, _, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = workflow.run(interrupt_after=["direct"])
    records = [
        json.loads(path.read_text())
        for path in (workflow.root / "evidence").glob("*.json")
    ]
    completed = next(
        item
        for item in records
        if item["kind"] == "node_result" and item["payload"]["node"] == "direct"
    )
    evidence_id = completed["evidence_id"]
    assert len(evidence_id) == 64
    session = session_for(case_graph)
    reader = next(tool for tool in build_tools(session) if tool.name == "read_evidence")
    page = reader.invoke({"evidence_id": evidence_id})
    assert page["kind"] == "node_result"
    saved = json.loads(page["content"])
    assert (
        saved["updates"].get("accepted_revision", state.accepted_revision)
        == state.accepted_revision
    )
    assert saved["node"] == "direct"
    assert page["next_offset"] is None
    assert not (workflow.root / "operations").exists()


def test_same_case_writer_lock_refuses_second_execution_without_running_agent(
    case_graph,
):
    workflow, _services, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    with (
        case_writer(workflow.root),
        pytest.raises(CaseResumeError, match="active writer"),
    ):
        workflow.run()
    assert workflow.runtime.agent.calls == 0
    assert not workflow.checkpoint_path.exists()
    assert workflow.run().completion_reason == "stable_correct"


def test_context_mismatch_refuses_before_checkpoint_or_evidence_changes(case_graph):
    workflow, _services, validator = make_workflow(
        case_graph, EditingAgent([[make_rule()]])
    )
    workflow.run(interrupt_after=["direct"])
    before = workflow.inspect()
    files = {
        path.relative_to(workflow.root): path.read_bytes()
        for path in (workflow.root / "evidence").glob("*.json")
    }
    resumed, services, _ = make_workflow(
        fresh_case_graph(case_graph), EditingAgent([]), validator
    )
    services.context_id = "different-context"
    with pytest.raises(
        CaseResumeError, match="changed source/data/config/prompt/workflow"
    ):
        resumed.run(resume=True)
    after = workflow.inspect()
    assert before.config == after.config
    assert before.values == after.values
    assert before.next == after.next
    assert files == {
        path.relative_to(workflow.root): path.read_bytes()
        for path in (workflow.root / "evidence").glob("*.json")
    }
    assert not (workflow.root / "operations").exists()
    assert resumed.runtime.agent.calls == 0


def test_schema_change_refuses_public_resume_before_evidence_changes(
    case_graph,
):
    from test_agent_runtime import ScriptModel

    from cortex.capabilities.sql_training.artifacts.jsonl import write_json_atomic
    from cortex.capabilities.sql_training.pipeline.train import TrainPipeline

    workflow, services, validator = make_workflow(
        case_graph, EditingAgent([[make_rule()]])
    )
    workflow.run(interrupt_after=["direct"])
    write_json_atomic(
        workflow.root / "case.json",
        {"source": workflow.runtime.record, "run_id": "schema-resume", "case_index": 1},
    )
    original = workflow.inspect()
    evidence = {
        path.name: path.read_bytes()
        for path in (workflow.root / "evidence").glob("*.json")
    }
    schema = workflow.runtime.graph.schema
    changed = schema.model_copy(
        update={
            "node_types": (
                schema.node_types[0].model_copy(
                    update={
                        "description": schema.node_types[0].description + " Updated."
                    }
                ),
                *schema.node_types[1:],
            )
        }
    )
    pipeline = TrainPipeline(
        services.config,
        model=ScriptModel([]),
        database=workflow.runtime.database,
        validation_client=validator,
        schema=changed,
    )
    with pytest.raises(
        CaseResumeError, match="changed source/data/config/prompt/workflow"
    ):
        pipeline.resume_case(workflow.root)
    after = workflow.inspect()
    assert original.config == after.config
    assert original.values == after.values
    assert original.next == after.next
    assert evidence == {
        path.name: path.read_bytes()
        for path in (workflow.root / "evidence").glob("*.json")
    }
    assert not (workflow.root / "operations").exists()
    assert not (workflow.root / "progress.json").exists()


def test_manual_resume_retries_only_unavailable_gate_without_repeating_direct(
    case_graph,
):
    from cortex.capabilities.sql_training.training.workflow.nodes import (
        ValidationUnavailable,
    )

    class OfflineValidator(Validator):
        offline = True
        retrieval_calls = 0

        def retrieve(self, **kwargs):
            self.retrieval_calls += 1
            if self.offline:
                raise ConnectionError("service offline")
            return super().retrieve(**kwargs)

    validator = OfflineValidator()
    workflow, services, _ = make_workflow(
        case_graph, EditingAgent([[make_rule()]]), validator
    )
    with pytest.raises(ValidationUnavailable):
        workflow.run()
    assert workflow.inspect().next == ("validate_initial",)
    assert validator.retrieval_calls == services.config.validation.max_retry_count + 1
    validator.offline = False
    resumed, _, _ = make_workflow(
        fresh_case_graph(case_graph), EditingAgent([]), validator
    )
    state = resumed.run(resume=True)
    assert state.completion_reason == "stable_correct"
    assert resumed.runtime.agent.calls == 0
    assert validator.generate_calls == 3


def test_existing_result_cannot_be_reused_for_different_completion(case_graph):
    from cortex.capabilities.sql_training.artifacts.cases import seal_case

    workflow, _, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = workflow.run()
    original = json.loads(Path(state.result_ref).read_text())
    changed = {
        **original,
        "correct": False,
        "asset_outcome": "partial",
        "completion_reason": "no_progress",
    }
    with pytest.raises(ValueError, match="existing case result"):
        seal_case(workflow.root, changed, workflow.runtime.graph)
    assert json.loads(Path(state.result_ref).read_text()) == original


@pytest.mark.parametrize("version", ["case-graph-v1", None])
def test_retired_checkpoint_layout_is_refused_before_current_schema_parse(
    case_graph, version
):
    workflow, _, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = workflow.run(interrupt_after=["direct"])
    retired = state.model_dump(mode="json")
    retired["bundle_ref"] = retired.pop("result_ref")
    if version is None:
        retired.pop("workflow_version")
    else:
        retired["workflow_version"] = version
    before = {
        path.name: path.read_bytes()
        for path in (workflow.root / "evidence").glob("*.json")
    }
    with pytest.raises(
        CaseResumeError, match="changed source/data/config/prompt/workflow"
    ):
        workflow._compatible(retired)
    assert before == {
        path.name: path.read_bytes()
        for path in (workflow.root / "evidence").glob("*.json")
    }


@pytest.mark.parametrize(
    ("query", "status", "calls"),
    [
        ("SELECT 0", "incorrect", 1),
        ("SELECT COUNT(*) FROM orders", "stable_correct", 3),
    ],
)
def test_completed_validation_survives_index_write_failure(
    case_graph, monkeypatch, query, status, calls
):
    validator = Validator(lambda _graph: query)
    workflow, _, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]), validator)
    write_index = EvidenceStore.write_index
    failed = False

    def crash(self, index):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("index write failed after validation evidence")
        write_index(self, index)

    monkeypatch.setattr(EvidenceStore, "write_index", crash)
    with pytest.raises(OSError, match="index write failed"):
        workflow.run()
    assert validator.generate_calls == calls
    assert workflow.inspect().next == ("validate_initial",)
    resumed, services, _ = make_workflow(
        fresh_case_graph(case_graph), EditingAgent([]), validator
    )
    # Inspect the recovered lookup without allowing any new generation; an
    # completed gate must keep its exact outcome after process recovery.
    revision = workflow.inspect().values["working_revision"]
    ref = services.validate(resumed.runtime.graph.read(revision))
    assert services.replay(ref).status.value == status
    assert validator.generate_calls == calls
    assert resumed.runtime.agent.calls == 0


def test_conflicting_completed_validation_evidence_is_rejected(case_graph):
    workflow, services, _ = make_workflow(case_graph, EditingAgent([[make_rule()]]))
    state = workflow.run(interrupt_after=["validate_initial"])
    saved = services.evidence.read(state.validation_ref)["payload"]
    conflicting = {**saved, "replay": {**saved["replay"], "correct": False}}
    services.evidence.put("validation", conflicting)
    with pytest.raises(ValueError, match="conflicting completed validation"):
        make_workflow(fresh_case_graph(case_graph), EditingAgent([]))


@pytest.mark.parametrize("crash_node", ["select", "validate_selected", "assess"])
def test_filtered_candidate_resume_keeps_old_acceptance_until_both_gates(
    case_graph, crash_node
):
    reviews = []

    def assessor(model, **payload):
        reviews.append(payload["trial"].revision)
        return assessment(
            payload["groups"], ["generation_rule.good"], payload["targets"], payload["validation"]
        )

    workflow, _, validator = make_workflow(
        case_graph,
        EditingAgent([[make_rule("good"), make_rule("bad")]]),
        assessor=assessor,
    )
    old = workflow.runtime.graph.accepted_revision
    # For assess interrupt once after filtering rather than on the first review.
    workflow.run(interrupt_after=["select"])
    if crash_node != "select":
        workflow.run(resume=True, interrupt_after=[crash_node])
    assert workflow.runtime.graph.accepted_revision == old
    resumed, _, _ = make_workflow(
        fresh_case_graph(case_graph), EditingAgent([]), validator, assessor
    )
    state = resumed.run(resume=True)
    assert state.completion_reason == "stable_correct"
    assert [a.id for a in resumed.runtime.graph.accepted.semantic_assets] == [
        "generation_rule.good"
    ]
    assert len(reviews) == 2
    assert validator.generate_calls == 6
    assert len(state.acceptance_refs) == 1
