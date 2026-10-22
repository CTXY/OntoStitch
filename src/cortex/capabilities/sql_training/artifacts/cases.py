"""One output bridge between case learning and the existing Incoming/Merge API."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
    write_jsonl,
)
from cortex.capabilities.sql_training.artifacts.workspace import (
    ASSET_GROUP_FILES,
    serialize_asset_envelope,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.case import project_case_edges
from cortex.capabilities.sql_training.graph.snapshots import (
    SEMANTIC_TYPES,
    GraphStore,
    fingerprint,
)
from cortex.capabilities.sql_training.training.services import CaseServices
from cortex.capabilities.sql_training.training.workflow.state import CaseState


def case_result(
    record: dict[str, Any],
    state: CaseState,
    graph: GraphStore,
    services: CaseServices,
    root: Path,
) -> dict[str, Any]:
    snapshot = graph.accepted
    assets = snapshot.semantic_assets
    ids = {a.id for a in assets}
    replay = None
    if state.validation_ref:
        payload = services.evidence.read(state.validation_ref)["payload"]
        if payload.get("content_fingerprint") == snapshot.content_fingerprint:
            replay = services.replay(state.validation_ref)
    stable = bool(replay and replay.correct)
    passed = stable and not state.operational_failure
    validation_status = replay.status.value if replay else "unavailable"
    accepted = {
        key
        for ref in state.acceptance_refs
        for key in services.evidence.read(ref)["payload"]["accepted_member_ids"]
    } & ids
    by_type = {
        kind: sorted(a.id for a in assets if a.node_type == kind)
        for kind in SEMANTIC_TYPES
    }
    evaluation = (
        replay.terminal_evaluation if replay else {"evaluation_available": False}
    )
    sem_hash = fingerprint(
        [a.model_dump(mode="json") for a in sorted(assets, key=lambda a: a.id)]
    )
    draft_edges = project_case_edges(assets)
    return {
        "source_id": str(record["source_id"]),
        "question": record["question"],
        "gold_sql": record["sql"],
        "query": evaluation.get("query"),
        "correct": passed,
        "sql_correct": stable,
        "case_stable_correct": stable,
        "case_status": "failed"
        if state.operational_failure
        else "passed"
        if passed
        else "needs_review",
        "validation_status": validation_status,
        "evaluation_available": validation_status != "unavailable",
        "failed_closed": state.operational_failure,
        "fatal": state.operational_failure,
        "completion_reason": state.completion_reason or "round_limit_reached",
        "reason": None if passed else state.completion_reason,
        "failure_reason": None if passed else state.completion_reason,
        "asset_outcome": "ready"
        if passed and assets
        else "partial"
        if assets
        else "empty",
        "direct_completed": state.direct_completed,
        "directly_retained_asset_ids": sorted(set(state.directly_retained_ids) & ids),
        "assessment_accepted_asset_ids": sorted(accepted),
        "accepted_metric_ids": by_type[NodeType.METRIC],
        "optimized_metric_ids": sorted(accepted & set(by_type[NodeType.METRIC])),
        "accepted_semantic_mapping_ids": by_type[NodeType.CONCEPT],
        "committed_semantic_mapping_ids": [],
        "accepted_rule_ids": by_type[NodeType.GENERATION_RULE],
        "accepted_asset_ids_by_type": {
            kind.value: values for kind, values in by_type.items()
        },
        "accepted_assets": len(assets),
        "ontology_updated": False,
        "draft_nodes_generated": bool(assets),
        "draft_nodes": [serialize_asset_envelope(a) for a in assets],
        "draft_node_ids": sorted(ids),
        "draft_node_count": len(assets),
        "draft_edge_ids": [e.id for e in draft_edges],
        "draft_edge_count": len(draft_edges),
        "final_ontology_fingerprint": sem_hash,
        "evaluation_ontology_fingerprint": sem_hash if replay else None,
        "final_graph_fingerprint": snapshot.content_fingerprint,
        "accepted_revision": snapshot.revision,
        "final_revision": snapshot.revision,
        "validation_ref": state.validation_ref,
        "acceptance_evidence_refs": state.acceptance_refs,
        "events": state.events,
        "iterations": sum("iteration" in e for e in state.events),
        "active_targets_complete": stable,
        "targets_complete": stable,
        "remaining_target_error_ids": []
        if stable
        else [t["target_id"] for t in state.targets],
        "replay_status": validation_status,
        "required_correct_replays": replay.required_correct_attempts
        if replay
        else services.config.train.required_consecutive_correct_replays,
        "completed_replays": len(replay.attempts) if replay else 0,
        "current_evaluation_summary": {
            k: evaluation.get(k)
            for k in ("correct", "query", "reason", "evaluation_status")
        },
        "calls": {
            "learning_model": state.model_calls,
            "tools": state.tool_calls,
            "database_investigations": state.database_calls,
            "assessment_rounds": len(state.acceptance_refs),
            "diagnosis_rounds": state.round_number,
        },
        "bundle_path": str(root / "bundle"),
        "checkpoint_path": str(root / "checkpoints.sqlite"),
        "thread_id": "case-" + fingerprint(record["source_id"]),
        "context_id": state.context_id,
        "workflow_version": state.workflow_version,
        "transaction_rolled_back": False,
    }


def bundle_signature(result: dict[str, Any]) -> str:
    return fingerprint(
        {
            key: result.get(key)
            for key in (
                "source_id",
                "context_id",
                "workflow_version",
                "final_revision",
                "final_graph_fingerprint",
                "validation_ref",
                "acceptance_evidence_refs",
                "case_status",
                "asset_outcome",
                "completion_reason",
                "correct",
                "direct_completed",
                "directly_retained_asset_ids",
                "failed_closed",
            )
        }
    )


def seal_case(root: Path, result: dict[str, Any], graph: GraphStore) -> dict[str, Any]:
    target = root / "bundle"
    if target.exists():
        previous = read_json(target / "case_result.json")
        if previous and bundle_signature(previous) == bundle_signature(result):
            return previous
        raise ValueError("existing case bundle belongs to different graph content")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=root) as tmp:
        staging = Path(tmp)
        assets = graph.accepted.semantic_assets
        for kind in SEMANTIC_TYPES:
            write_json_atomic(
                staging / "assets" / ASSET_GROUP_FILES[kind],
                [serialize_asset_envelope(a) for a in assets if a.node_type == kind],
            )
        write_json_atomic(staging / "draft_nodes.json", result["draft_nodes"])
        write_jsonl(
            staging / "draft_edges.jsonl",
            [e.model_dump(mode="json") for e in project_case_edges(assets)],
        )
        write_json_atomic(staging / "case_result.json", result)
        write_json_atomic(
            staging / "manifest.json",
            {
                "schema_version": 1,
                "source_id": result["source_id"],
                "asset_outcome": result["asset_outcome"],
                "merge_eligible": result["asset_outcome"] == "ready",
                "snapshot_revision": graph.accepted_revision,
                "content_fingerprint": result["final_graph_fingerprint"],
                "checkpoint_path": "../checkpoints.sqlite",
                "thread_id": result["thread_id"],
                "context_id": result["context_id"],
                "workflow_version": result["workflow_version"],
                "snapshots_path": "../snapshots",
                "evidence_path": "../evidence",
                "draft_nodes_path": "draft_nodes.json",
            },
        )
        staging.replace(target)
    return result
