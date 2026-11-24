"""Task revisions and explicit migrations keep accepted merge progress intact."""

from copy import deepcopy

import pytest

from cortex.capabilities.graph_merge.runtime.state import (
    CheckpointContractMismatch,
    ContractVersions,
    TaskState,
    validate_checkpoint_contract,
)
from cortex.capabilities.graph_merge.runtime.workspace import WorkspaceStore
from cortex.schema import GraphNode, GraphSpace, GraphState


def graph(name="original"):
    return GraphState(graph=GraphSpace(nodes=(GraphNode(id="subject", node_type="concept", name=name),)))


def versions():
    return ContractVersions(schema="ontology", tools="patch-v2", prompts="role-prompts", review_policy="preservation")


def legacy_snapshot():
    return {"current": graph().model_dump(mode="json"), "lineage": {"version": 1, "groups": []},
        "history": [{"task_id": "content-0", "accepted": True}], "accepted_revision": 1,
        "open_tasks": [{"id": "structure-1-4", "phase": "reconcile", "targets": ["subject"]}],
        "input_fingerprint": "original-source-fingerprint", "identity": {"groups": [], "distinctions": []},
        "identity_read_ids": ["subject"], "content_plan": [[{"members": ["subject"]}]],
        "relation_coverage": ["accepted-region"], "reconciliation_complete": False,
        "pending_review": {"stage": "assess", "verdict": "accept"},
        "pending_relation_work": {"task_id": "structure-1-4", "state": graph("unaccepted").model_dump(mode="json"),
                                  "review_history": [{"verdict": "accept"}]}, "final": False}


def test_task_state_detaches_graphs_and_rejects_stale_reviews():
    base = graph()
    task = TaskState.capture(base, task_id="local", phase="reconcile", versions=versions())
    proposed = {"node_updates": [{"id": "subject", "name": "candidate"}]}
    drafted = task.with_draft(proposed)
    proposed["node_updates"].clear()
    assert drafted.draft["node_updates"]
    assert task.draft is None and task.base == base.model_dump(mode="json")
    candidate = drafted.with_candidate(graph("candidate"))
    with pytest.raises(ValueError, match="current candidate"):
        candidate.with_review({"verdict": "accept", "issues": []}, candidate_hash=task.base_hash)
    accepted = candidate.with_review({"verdict": "accept", "issues": []}, candidate_hash=candidate.candidate_hash)
    assert accepted.stage == "ready_to_commit"
    assert TaskState.from_dict(accepted.to_dict()) == accepted
    repaired = accepted.with_draft({"explanation": "Replace the task draft"})
    assert repaired.review is None
    assert repaired.base_hash == task.base_hash
    assert repaired.last_valid_candidate == candidate.last_valid_candidate
    assert repaired.stage == "validating"  # Last valid candidate is not the new proposal.
    with pytest.raises(ValueError, match="current candidate"):
        repaired.with_review({"verdict": "accept", "issues": []}, candidate_hash=candidate.candidate_hash)


@pytest.mark.parametrize("field", ["base", "last_valid_candidate", "draft"])
def test_serialized_task_detects_changed_payloads(field):
    task = TaskState.capture(graph(), task_id="local", phase="reconcile", versions=versions())
    task = task.with_draft({"explanation": "candidate"}).with_candidate(graph("candidate"))
    raw = task.to_dict()
    if field == "draft":
        raw[field]["explanation"] = "tampered"
    else:
        raw[field]["graph"]["nodes"][0]["name"] = "tampered"
    with pytest.raises(ValueError, match="hash mismatch"):
        TaskState.from_dict(raw)


def test_contract_mismatch_requires_explicit_migration():
    with pytest.raises(CheckpointContractMismatch, match="explicit migration"):
        validate_checkpoint_contract(legacy_snapshot(), versions())
    raw = {"contract_versions": versions().to_dict()}
    assert validate_checkpoint_contract(raw, versions()) == versions()
    different = ContractVersions(**{**versions().to_dict(), "review_policy": "new-policy"})
    with pytest.raises(CheckpointContractMismatch, match="review_policy"):
        validate_checkpoint_contract(raw, different)


def test_workspace_checks_task_contract_and_state_integrity(tmp_path):
    payload = legacy_snapshot()
    task = TaskState.capture(graph(), task_id="local", phase="reconcile", versions=versions())
    payload.update(task_state=task.to_dict(), contract_versions=versions().to_dict())
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.save("merge", payload)
    wrong = deepcopy(payload)
    wrong["contract_versions"]["prompts"] = "different"
    with pytest.raises(ValueError, match="contracts differ"):
        store.save("merge", wrong, expected_revision=1)
    assert store.load("merge")["revision"] == 1
    unrelated = deepcopy(payload)
    unrelated['current'] = graph('different accepted base').model_dump(mode='json')
    with pytest.raises(ValueError, match='accepted workspace'):
        store.save('merge', unrelated, expected_revision=1)
