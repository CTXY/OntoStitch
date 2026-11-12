"""One output bridge between case learning and the existing Incoming/Merge API."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.artifacts.workspace import (
    deserialize_asset_envelope,
    serialize_asset_envelope,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.snapshots import (
    SEMANTIC_TYPES,
    GraphStore,
    fingerprint,
)
from cortex.capabilities.sql_training.models import NodeAsset
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
    passed = stable and state.direct_completed and not state.operational_failure
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
    draft_edges = tuple(
        e
        for e in graph.validation_graph(snapshot.revision).edges
        if e.from_node_id in ids
    )
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
        "accepted_event_ids": by_type[NodeType.EVENT],
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
        "draft_node_ids": sorted(ids),
        "draft_node_count": len(assets),
        "draft_edges": [e.model_dump(mode="json") for e in draft_edges],
        "relation_contracts": [c.model_dump(mode="json") for c in snapshot.graph.relation_contracts],
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
        "result_path": str(root / "result" / "result.json"),
        "ontology_path": str(root / "result" / "ontology.json"),
        "checkpoint_path": str(root / "checkpoints.sqlite"),
        "thread_id": "case-" + fingerprint(record["source_id"]),
        "context_id": state.context_id,
        "workflow_version": state.workflow_version,
        "transaction_rolled_back": False,
    }


def result_signature(result: dict[str, Any]) -> str:
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
    if graph.accepted.conflict_nodes:
        raise ValueError(
            "Resolve accepted conflicts before exporting the single-definition case ontology: "
            + ", ".join(c.id for c in graph.accepted.conflict_nodes)
        )
    target = root / "result"
    if target.exists():
        previous = read_json(target / "result.json")
        if previous and result_signature(previous) == result_signature(result):
            load_case_assets(previous, case_root=root)
            return previous
        raise ValueError("existing case result belongs to different completion")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".result-", dir=root) as tmp:
        staging = Path(tmp)
        assets = graph.accepted.semantic_assets
        write_json_atomic(
            staging / "ontology.json", [serialize_asset_envelope(a) for a in assets]
        )
        write_json_atomic(staging / "result.json", result)
        staging.replace(target)
    return result


def load_case_assets(
    result: dict[str, Any], *, case_root: Path | None = None
) -> list[NodeAsset]:
    """Read the sole semantic export; report metadata never embeds graph content."""
    path = (
        case_root / "result" / "ontology.json"
        if case_root is not None
        else result.get("ontology_path")
    )
    if not path:
        raise ValueError("case has no formal ontology export")
    rows = read_json(Path(path))
    if not isinstance(rows, list):
        raise TypeError("case ontology export must be an array")
    assets = [deserialize_asset_envelope(row) for row in rows]
    if any(asset.node_type not in SEMANTIC_TYPES for asset in assets):
        raise ValueError("case export must contain only semantic assets")
    actual = fingerprint(
        [a.model_dump(mode="json") for a in sorted(assets, key=lambda a: a.id)]
    )
    if result.get("final_ontology_fingerprint") != actual:
        raise ValueError("case ontology export fingerprint mismatch")
    if len({asset.id for asset in assets}) != len(assets):
        raise ValueError("duplicate assets in case ontology export")
    return assets
