"""Task revisions and explicit migrations keep accepted merge progress intact."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortex.capabilities.graph_merge.integration.checkpoint_migration import migrate_checkpoint
from cortex.capabilities.graph_merge.integration.task_state import (
    CheckpointContractMismatch,
    ContractVersions,
    TaskState,
    canonical_hash,
    validate_checkpoint_contract,
)
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
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


def test_legacy_migration_keeps_accepted_progress_and_archives_old_reviews(tmp_path):
    source = tmp_path / "source/workspace.sqlite"
    original = legacy_snapshot()
    store = WorkspaceStore(source)
    store.save("train-merge-batch-0003", original)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "new-output/batch-0003/workspace.sqlite"
    migrated = migrate_checkpoint(source, target, versions=versions())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    for field in ("current", "lineage", "history", "accepted_revision", "identity", "identity_read_ids",
                  "content_plan", "relation_coverage", "input_fingerprint"):
        assert migrated[field] == original[field]
    assert migrated["pending_review"] is None and migrated["pending_relation_work"] is None
    task = TaskState.from_dict(migrated["task_state"])
    assert task.recovery == "needs_rebuild" and task.task_id == "structure-1-4"
    assert task.base == original["current"] and task.last_valid_candidate is None
    assert migrated["open_tasks"][0]["id"] == "structure-1-4"
    assert migrated["open_tasks"][0]["feedback"] == []
    assert migrated["open_tasks"][0]["aliases"] == {}
    archived = json.loads((target.parent / "migration_source.json").read_text())
    assert archived["snapshot"]["pending_relation_work"] == original["pending_relation_work"]
    assert migrated["migration"]["reviews_reused"] is False
    assert validate_checkpoint_contract(migrated, versions()) == versions()
    with pytest.raises(ValueError, match="new destination"):
        migrate_checkpoint(source, target, versions=versions())
    with pytest.raises(ValueError, match="new output directory"):
        migrate_checkpoint(source, source.with_name("other.sqlite"), versions=versions())


def _script(name):
    path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_training_migration_preserves_prefix_fingerprints_and_requires_matching_plan(tmp_path):
    script = _script("migrate_merge_checkpoint")
    runner = _script("run_resilient_training")
    source, target = tmp_path / "source", tmp_path / "copy"
    case = source / "cases/1"
    case.mkdir(parents=True)
    (source / "manifest.json").write_text(json.dumps({"question_ids": ["1"]}))
    (source / "private.keys").write_text("do-not-copy")
    (case / "state.json").write_text(graph().model_dump_json())
    (case / "input.json").write_text(json.dumps({"source_id": "case-1"}))
    first = source / "merge/batch-0001"
    first.mkdir(parents=True)
    (first / "state.json").write_text(graph().model_dump_json())
    (first / "audit.json").write_text(json.dumps({"status": "passed"}))
    fingerprint = '{"sha256":"unchanged-prefix-input"}'
    (first / "input_fingerprint.json").write_text(fingerprint)
    unfinished = source / "merge/batch-0002/workspace.sqlite"
    legacy = legacy_snapshot()
    legacy["pending_relation_work"]["task_id"] = "structure-1-4"
    WorkspaceStore(unfinished).save("train-merge-batch-0002", legacy)
    plan = script.migrate_training_output(source, target, versions=versions())
    assert (target / "merge/batch-0001/input_fingerprint.json").read_text() == fingerprint
    assert (target / "merge/batch-0001/state.json").read_bytes() == (first / "state.json").read_bytes()
    assert not (target / "private.keys").exists()
    snapshot = WorkspaceStore(target / "merge/batch-0002/workspace.sqlite").load("train-merge-batch-0002")
    args = SimpleNamespace(output=target, merge_checkpoint_migration=target / "merge_checkpoint_migration.json")
    runner.validate_merge_migration(args, "batch-0002", snapshot)
    runner.validate_merge_migration(args, "batch-0003", None)
    assert plan["checkpoints"]["batch-0002"]["accepted_graph_hash"] == canonical_hash(snapshot["current"])
    with pytest.raises(ValueError, match="requires --merge-checkpoint-migration"):
        runner.validate_merge_migration(SimpleNamespace(output=target), "batch-0002", snapshot)
    bad = deepcopy(snapshot)
    bad["current"]["graph"]["nodes"][0]["name"] = "tampered"
    with pytest.raises(ValueError, match="accepted graph"):
        runner.validate_merge_migration(args, "batch-0002", bad)


@pytest.mark.parametrize('mode', ['case', 'supervise'])
def test_migrated_outputs_cannot_start_extraction(mode, tmp_path, monkeypatch):
    runner = _script('run_resilient_training')
    monkeypatch.setattr('sys.argv', ['run_resilient_training.py', '--mode', mode,
        '--output', str(tmp_path / 'copy'), '--config', str(tmp_path / 'config.yaml'),
        '--keys', str(tmp_path / 'keys'), '--merge-checkpoint-migration', str(tmp_path / 'plan.json')])
    with pytest.raises(SystemExit) as stopped:
        runner.main()
    assert stopped.value.code == 2


def test_failed_output_copy_does_not_publish_partial_destination(tmp_path):
    script = _script('migrate_merge_checkpoint')
    source, target = tmp_path / 'source', tmp_path / 'copy'
    (source / 'cases').mkdir(parents=True)
    (source / 'manifest.json').write_text(json.dumps({'question_ids': []}))
    batch = source / 'merge/batch-0001'
    batch.mkdir(parents=True)
    (batch / 'state.json').write_text(graph().model_dump_json())
    with pytest.raises(ValueError, match='passed audit'):
        script.migrate_training_output(source, target, versions=versions())
    assert not target.exists()
    assert not list(tmp_path.glob('.copy-migration-*'))
    assert (batch / 'state.json').exists()
