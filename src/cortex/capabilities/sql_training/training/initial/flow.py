from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import Field

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.llm import LLMProvider
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.initial.compositions import (
    build_initial_candidate_compositions,
)
from cortex.capabilities.sql_training.training.repair.contracts import (
    asset_fingerprint,
    ontology_fingerprint,
)
from cortex.capabilities.sql_training.training.replay import StableReplayResult

_INITIAL_CANDIDATE_TYPES = {
    NodeType.METRIC,
    NodeType.SEMANTIC_MAPPING,
    NodeType.GENERATION_RULE,
}
_SEMANTIC_TYPES = _INITIAL_CANDIDATE_TYPES | {NodeType.GENERATION_RULE}


class InitialCasePartition(NodeModel):
    """Simple Initial boundary: accepted Working, candidates, and not-accepted IDs."""

    working_assets: list[NodeAsset] = Field(default_factory=list)
    initial_candidates: list[NodeAsset] = Field(default_factory=list)
    not_accepted_asset_ids: list[str] = Field(default_factory=list)
    deduplicated_asset_ids: list[str] = Field(default_factory=list)


class InitialFlowResult(NodeModel):
    """Directly retained Working and its stable replay diagnostic cursor."""

    working_assets: list[NodeAsset] = Field(default_factory=list)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    accepted_asset_ids: list[str] = Field(default_factory=list)
    rejected_asset_ids: list[str] = Field(default_factory=list)
    deduplicated_asset_ids: list[str] = Field(default_factory=list)
    requires_repair: bool = False
    failure_reason: str | None = None


ReplayInitialStage = Callable[[str, list[NodeAsset]], StableReplayResult]
PublishWorking = Callable[[list[NodeAsset]], None]
TraceInitialStep = Callable[..., None]


def run_initial_flow(
    *,
    record: dict[str, Any],
    staged_assets: list[NodeAsset],
    provider: LLMProvider,
    replay: ReplayInitialStage,
    publish_working: PublishWorking,
    trace: TraceInitialStep,
    metric_candidate_sets: list[dict[str, Any]] | None = None,
) -> InitialFlowResult:
    """Retain direct extraction and evaluate it once through the stable replay gate.

    Accepted IDs mean retained knowledge, not an initial semantic assessment verdict.
    The replay callback builds and validates the actual case retrieval graph.
    """

    del provider  # Kept in the call contract; initial extraction has no LLM assessment.
    partition = build_initial_case_partition(
        case_id=str(record["source_id"]),
        permanent_base_assets=[],
        merged_assets=staged_assets,
    )
    candidates = partition.initial_candidates
    compositions = build_initial_candidate_compositions(
        candidates, metric_candidate_sets=metric_candidate_sets,
    )
    working = _overlay_assets(partition.working_assets, candidates)
    retained_ids = {asset.id for asset in working}
    claims: dict[str, dict[str, list[str]]] = {}
    for asset in candidates:
        if asset.node_type in {NodeType.SEMANTIC_MAPPING, NodeType.METRIC}:
            name = " ".join(asset.name.split()).casefold()
            if name:
                claims.setdefault(name, {}).setdefault(asset.node_type.value, []).append(asset.id)
    conflicts = {
        name: sorted(asset_id for ids in types.values() for asset_id in ids)
        for name, types in claims.items() if len(types) > 1
    }
    publish_working(working)
    trace(
        step="initial_direct_retention",
        phase="initial_working_replay",
        input={"staged_asset_ids": [asset.id for asset in staged_assets]},
        output={
            "retained_asset_ids": sorted(retained_ids),
            "retention_basis": "direct_extraction",
            "initial_candidate_compositions": [
                {
                    "candidate_id": item.candidate_id,
                    "root_asset_ids": list(item.root_asset_ids),
                    "dependency_asset_ids": list(item.dependency_asset_ids),
                    "asset_ids": list(item.asset_ids),
                }
                for item in compositions
            ],
            "same_name_concept_metric_conflicts": conflicts,
        },
        summary={
            "retained_asset_count": len(retained_ids),
            "working_ontology_fingerprint": ontology_fingerprint(working),
            "semantic_assessment_performed": False,
        },
    )
    replay_result = replay("initial_working_replay", working)
    evaluation = dict(replay_result.terminal_evaluation)
    evaluation["ontology_fingerprint"] = ontology_fingerprint(working)
    event = _replay_event(replay_result, ontology="retained_working", stage="initial_direct_extraction")
    event.update(
        retained_asset_ids=sorted(retained_ids),
        retention_basis="direct_extraction",
        ontology_fingerprint=ontology_fingerprint(working),
        same_name_concept_metric_conflicts=conflicts,
    )
    failure = evaluation.get("candidate_failure") or {}
    if evaluation.get("evaluation_available") is False:
        reason = "initial_working_evaluation_unavailable"
    elif failure.get("kind") == "generation" or not str(evaluation.get("query") or "").strip():
        reason = "initial_working_generation_failed"
    else:
        reason = None
    return _initial_result(
        working=working,
        evaluation=evaluation,
        events=[event],
        accepted_ids=retained_ids,
        rejected_ids=set(partition.not_accepted_asset_ids),
        deduplicated_ids=partition.deduplicated_asset_ids,
        requires_repair=reason is None and not replay_result.correct,
        failure_reason=reason,
    )


def build_initial_case_partition(
    *,
    case_id: str,
    permanent_base_assets: list[NodeAsset],
    merged_assets: list[NodeAsset],
    candidate_outcomes: list[dict] | None = None,
) -> InitialCasePartition:
    """Separate extracted candidates from accepted Working without a revision ledger."""

    del case_id, candidate_outcomes
    _validate_semantic_assets(permanent_base_assets, label="accepted Working")
    _validate_semantic_assets(merged_assets, label="merged ontology")
    working_by_id = _unique_assets(permanent_base_assets, label="accepted Working")
    merged_by_id = _unique_assets(merged_assets, label="merged ontology")
    missing_ids = sorted(set(working_by_id) - set(merged_by_id))
    if missing_ids:
        raise ValueError(f"initial Merge result removed accepted Working assets: {missing_ids}")

    candidates: list[NodeAsset] = []
    not_accepted: set[str] = set()
    deduplicated: set[str] = set()
    for asset in sorted(merged_assets, key=lambda row: (row.node_type.value, row.id)):
        existing = working_by_id.get(asset.id)
        if existing is not None:
            if asset_fingerprint(existing) == asset_fingerprint(asset):
                deduplicated.add(asset.id)
            else:
                # Initial never performs a generalized UPDATE of accepted knowledge.
                not_accepted.add(asset.id)
            continue
        if asset.node_type in _INITIAL_CANDIDATE_TYPES:
            candidates.append(asset.model_copy(deep=True))
        else:
            not_accepted.add(asset.id)

    return InitialCasePartition(
        working_assets=[asset.model_copy(deep=True) for asset in permanent_base_assets],
        initial_candidates=candidates,
        not_accepted_asset_ids=sorted(not_accepted),
        deduplicated_asset_ids=sorted(deduplicated),
    )


def _validate_semantic_assets(assets: list[NodeAsset], *, label: str) -> None:
    invalid = sorted(
        {asset.node_type.value for asset in assets if asset.node_type not in _SEMANTIC_TYPES}
    )
    if invalid:
        raise ValueError(f"{label} contains non-semantic assets: {invalid}")


def _unique_assets(assets: list[NodeAsset], *, label: str) -> dict[str, NodeAsset]:
    by_id: dict[str, NodeAsset] = {}
    for asset in assets:
        if asset.id in by_id:
            raise ValueError(f"{label} contains duplicate asset ID: {asset.id}")
        by_id[asset.id] = asset
    return by_id


def _overlay_assets(base: list[NodeAsset], updates: list[NodeAsset]) -> list[NodeAsset]:
    by_id = {asset.id: asset.model_copy(deep=True) for asset in base}
    for asset in updates:
        by_id[asset.id] = asset.model_copy(deep=True)
    return sorted(by_id.values(), key=lambda asset: asset.id)


def _replay_event(
    replay: StableReplayResult,
    *,
    ontology: str,
    stage: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "round": 0,
        "evaluation": replay.terminal_evaluation,
        "replay_attempts": [
            {
                "attempt": attempt.attempt,
                "trace_id": attempt.trace_id,
                "evaluation": _evaluation_summary(attempt.evaluation),
            }
            for attempt in replay.attempts
        ],
        "ontology": ontology,
    }
    if stage is not None:
        event["stage"] = stage
    return event


def _evaluation_summary(evaluation: dict[str, Any] | None) -> dict[str, Any]:
    if not evaluation:
        return {}
    keys = (
        "correct",
        "evaluation_available",
        "query",
        "reason",
        "replay_status",
        "required_correct_replays",
        "completed_replays",
        "first_failure_attempt",
        "error",
    )
    return {key: evaluation.get(key) for key in keys if key in evaluation}


def _initial_result(
    *,
    working: list[NodeAsset],
    evaluation: dict[str, Any],
    events: list[dict[str, Any]],
    accepted_ids: set[str],
    rejected_ids: set[str],
    deduplicated_ids: list[str],
    requires_repair: bool = False,
    failure_reason: str | None = None,
) -> InitialFlowResult:
    return InitialFlowResult(
        working_assets=working,
        evaluation=evaluation,
        events=events,
        accepted_asset_ids=sorted(accepted_ids),
        rejected_asset_ids=sorted(rejected_ids),
        deduplicated_asset_ids=sorted(deduplicated_ids),
        requires_repair=requires_repair,
        failure_reason=failure_reason,
    )
