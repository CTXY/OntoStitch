"""A mechanically invalid repair cannot erase an outstanding semantic defect."""
from dataclasses import replace

from cortex.capabilities.graph_merge.integration.task_state import ContractVersions, TaskState
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.schema import GraphNode, GraphSpace, GraphState


def graph(name):
    return GraphState(graph=GraphSpace(nodes=(GraphNode(id="degree", node_type="concept", name=name),)))


def reviewed_task():
    versions = ContractVersions(schema="ontology", tools="source-units", prompts="conflict-contract", review_policy="preserve")
    base = graph("thrombosis degree")
    state = TaskState.capture(base, task_id="content-1", phase="consolidate", versions=versions)
    state = state.with_draft({"explanation": "initial integration"}).with_candidate(graph("incorrectly unified degree"))
    review = {"verdict": "revise", "issues": [{
        "refs": [{"kind": "node", "id": "degree", "view": "candidate", "fields": ["name"]}],
        "basis": [{"kind": "node", "id": "degree", "view": "before", "fields": ["name"]}],
        "problem": "The two severity orderings have been collapsed.",
        "required_outcome": "Retain both supported severity interpretations.",
    }]}
    # The controller captures exact prior-candidate records referred to by a
    # finding; TaskState transports these records without graph lookup.
    state = replace(state, semantic_issue_basis=({
        "candidate_hash": state.candidate_hash,
        "ref": review["issues"][0]["refs"][0],
        "record": {"id": "degree", "name": "incorrectly unified degree"},
    },))
    return base, state.with_review(review, candidate_hash=state.candidate_hash)


def test_semantic_defect_survives_mechanical_repair_failure_and_process_restart(tmp_path):
    base, reviewed = reviewed_task()
    semantic = reviewed.semantic_issues
    basis = reviewed.semantic_issue_basis
    assert semantic and basis
    # The next attempt is not an accepted candidate; its local format problem
    # must coexist with the previously observed semantic preservation defect.
    broken = reviewed.with_draft({"explanation": "repair branch routing"})
    broken = broken.with_issues([{"code": "unexpected_source_unit", "extra": ["owner/null"]}], stage="validating")
    assert broken.review is None
    assert broken.issues != semantic
    assert broken.semantic_issues == semantic
    assert broken.semantic_issue_basis == basis
    payload = {"current": base.model_dump(mode="json"), "lineage": {},
               "history": [{"task_id": "content-0", "revision": 1}], "open_tasks": [],
               "contract_versions": broken.versions.to_dict(), "task_state": broken.to_dict()}
    store = WorkspaceStore(tmp_path / "workspace.sqlite")
    store.save("merge", payload)
    restored = TaskState.from_dict(store.load("merge")["task_state"])
    assert restored.semantic_issues == semantic
    assert restored.semantic_issue_basis == basis
    assert restored.issues == broken.issues
    assert restored.last_valid_candidate == reviewed.last_valid_candidate
    # Successful mechanical recompilation is still not a semantic acceptance.
    compiled = restored.with_draft({"explanation": "valid revised integration"}).with_candidate(graph("two retained interpretations"))
    assert compiled.semantic_issues == semantic
    assert compiled.semantic_issue_basis == basis
    accepted = compiled.with_review({"verdict": "accept", "issues": []}, candidate_hash=compiled.candidate_hash)
    assert not accepted.semantic_issues
    assert not accepted.semantic_issue_basis
    assert accepted.stage == "ready_to_commit"


def test_same_base_new_candidate_never_reuses_old_semantic_acceptance():
    _, reviewed = reviewed_task()
    candidate = reviewed.with_draft({"explanation": "first correction"}).with_candidate(graph("corrected degree"))
    accepted = candidate.with_review({"verdict": "accept", "issues": []}, candidate_hash=candidate.candidate_hash)
    next_draft = accepted.with_draft({"explanation": "different draft"})
    assert next_draft.review is None
    assert next_draft.stage == "validating"
    assert not next_draft.semantic_issues
    assert next_draft.base_hash == accepted.base_hash


def test_context_request_does_not_adjudicate_previous_semantic_defect():
    _, reviewed = reviewed_task()
    candidate = reviewed.with_draft({"explanation": "attempted correction"}).with_candidate(graph("candidate needs more context"))
    request = {"verdict": "needs_context", "issues": [{
        "refs": [{"kind": "node", "id": "degree", "view": "before", "fields": ["name"]}],
        "problem": "Need complete original severity conditions before judging the repair.",
        "required_outcome": "Read the complete conditions.",
    }]}
    waiting = candidate.with_review(request, candidate_hash=candidate.candidate_hash)
    assert waiting.semantic_issues == reviewed.semantic_issues
    assert waiting.semantic_issue_basis == reviewed.semantic_issue_basis
    assert waiting.issues == tuple(request["issues"])
    assert waiting.stage == "assessing"
