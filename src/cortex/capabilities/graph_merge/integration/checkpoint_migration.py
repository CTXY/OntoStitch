"""Explicit, conservative migration of a stopped checkpoint into a new file.

The accepted graph and its history are retained. Old unapplied proposals are
archived, never reinterpreted as new Patch objects or accepted reviews.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from cortex.capabilities.graph_merge.integration.task_state import (
    ContractVersions,
    TaskState,
    canonical_hash,
)
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.schema import GraphState


def _read_snapshot(source, run_id):
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT run_id, revision, payload FROM merge_workspaces").fetchall()
    if run_id is None:
        if len(rows) != 1:
            raise ValueError("Select run_id explicitly for a checkpoint containing multiple runs")
        selected = rows[0]
    else:
        selected = next((row for row in rows if row[0] == run_id), None)
        if selected is None:
            raise ValueError(f"No checkpoint for run {run_id!r}")
    ident, revision, encoded = selected
    payload = json.loads(encoded)
    if payload.get("revision") != revision:
        raise ValueError("Source checkpoint row and payload revisions disagree")
    GraphState.model_validate(payload["current"])
    return ident, revision, payload


def migrate_checkpoint(source, destination, *, versions: ContractVersions, run_id=None):
    """Create a new workspace, preserving accepted progress without rewriting hashes.

    Recovery of an old local proposal is deliberately conservative: the new
    runtime rebuilds only that unfinished task from accepted current. Its full
    old candidate and review remain in the accompanying source artifact.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists() or destination == source:
        raise ValueError("Checkpoint migration requires a new destination file")
    if destination.parent == source.parent:
        raise ValueError("Migrate into a new output directory, not the source checkpoint directory")
    source_hash = sha256(source.read_bytes()).hexdigest()
    ident, old_revision, payload = _read_snapshot(source, run_id)
    original = deepcopy(payload)
    identity = payload.get("identity") or {}
    recovered_reads = False
    if payload.get("identity_read_ids") is None and (identity.get("groups") or identity.get("distinctions")):
        from cortex.capabilities.graph_merge.integration.identity_reads import identity_reads_from_trace

        trace = source.parent / "agents.jsonl"
        if not trace.exists():
            raise ValueError("Legacy identity has no read proof; its matching original Agent trace is required for migration")
        payload["identity_read_ids"] = identity_reads_from_trace(trace, identity)
        recovered_reads = True
    accepted = GraphState.model_validate(payload["current"])
    pending = payload.get("pending_relation_work") or payload.get("pending_review")
    open_tasks = payload.get("open_tasks", [])
    task_id = pending.get("task_id") if pending else (open_tasks[0].get("id") if open_tasks else None)
    phase = pending.get("phase", "reconcile") if pending else (open_tasks[0].get("phase") if open_tasks else None)
    if pending and payload.get("pending_relation_work"):
        phase = "reconcile"
    if phase is None and task_id:
        raise ValueError("Unfinished legacy task lacks a phase")
    if task_id:
        definition = deepcopy(next((task for task in open_tasks if task.get("id") == task_id), {}))
        definition.update(id=task_id, phase=phase)
        for field in ("targets", "groups", "checks"):
            definition[field] = deepcopy((pending or {}).get(field, definition.get(field, [])))
        if phase == "consolidate" and not definition["groups"] and task_id.startswith("content-"):
            index = int(task_id.removeprefix("content-"))
            plan = payload.get("content_plan") or []
            if index >= len(plan):
                raise ValueError("Legacy content task has no corresponding saved content plan")
            definition["groups"] = deepcopy(plan[index])
        present = {item.id for items in (accepted.graph.nodes, accepted.graph.edges) for item in items}
        definition["targets"] = [key for key in definition["targets"] if key in present]
        # Findings about the discarded candidate are historical observations,
        # not requirements on accepted B. In particular, B may already retain
        # the metadata an old candidate lost.
        definition["feedback"] = []
        definition["aliases"] = {}  # Re-derived from the retained IdentityPlan.
        payload["open_tasks"] = [definition, *[task for task in open_tasks if task.get("id") != task_id]]
    payload.pop("revision", None)
    payload["contract_versions"] = versions.to_dict()
    payload["pending_review"] = None
    payload["pending_relation_work"] = None
    payload["final"] = False
    payload["task_state"] = None
    if task_id:
        task = TaskState.capture(accepted, task_id=task_id, phase=phase, versions=versions)
        # Old candidate deltas are not new complete patches. Only accepted
        # current is eligible as the base for a new writer invocation.
        task_data = task.to_dict()
        task_data["recovery"] = "needs_rebuild"
        payload["task_state"] = TaskState.from_dict(task_data).to_dict()
    payload["migration"] = {
        "version": 1,
        "source": str(source),
        "source_sha256": source_hash,
        "source_payload_hash": canonical_hash(original),
        "source_run_id": ident,
        "source_storage_revision": old_revision,
        "source_input_fingerprint": original.get("input_fingerprint"),
        "accepted_graph_hash": canonical_hash(original["current"]),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pending_recovery": "needs_rebuild" if task_id else "none",
        "reviews_reused": False,
        "legacy_feedback_archived": True,
        "identity_reads_recovered": recovered_reads,
        "source_artifact": "migration_source.json",
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    artifact = destination.parent / "migration_source.json"
    if artifact.exists():
        raise ValueError("Migration source artifact already exists")
    artifact.write_text(json.dumps({"run_id": ident, "snapshot": original}, ensure_ascii=False, indent=2) + "\n")
    store = WorkspaceStore(destination)
    store.save(ident, payload)
    migrated = store.load(ident)
    # The original input fingerprint is a historical fact, not a value to
    # overwrite with the current runtime's expected fingerprint.
    assert migrated["input_fingerprint"] == original["input_fingerprint"]
    assert migrated["current"] == original["current"]
    assert migrated["history"] == original["history"]
    if sha256(source.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("Source checkpoint changed during migration; discard the new output")
    return migrated
