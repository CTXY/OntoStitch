from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import Field

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.llm import LLMProvider
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.models.generation_rule import GenerationRuleProperties
from cortex.capabilities.sql_training.training.initial.assessment import (
    InitialCandidateAssessmentError,
    assess_initial_candidates,
    build_initial_candidate_compositions,
    build_initial_candidate_decision,
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
    """Accepted Working and the diagnostic cursor produced by Initial validation."""

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
    """Validate extracted candidates through induction assessment and retention."""

    partition = build_initial_case_partition(
        case_id=str(record["source_id"]),
        permanent_base_assets=[],
        merged_assets=staged_assets,
        candidate_outcomes=None,
    )
    working = [asset.model_copy(deep=True) for asset in partition.working_assets]
    candidates = [asset.model_copy(deep=True) for asset in partition.initial_candidates]
    candidates_by_id = {asset.id: asset for asset in candidates}
    candidate_compositions = build_initial_candidate_compositions(
        candidates,
        metric_candidate_sets=metric_candidate_sets,
    )
    compositions_by_id = {candidate.candidate_id: candidate for candidate in candidate_compositions}
    rejected_ids = set(partition.not_accepted_asset_ids)
    accepted_ids: set[str] = set()
    events: list[dict[str, Any]] = []
    publish_working(working)
    trace(
        step="initial_candidate_partition",
        phase="initial_induction_replay",
        input={"staged_asset_ids": [asset.id for asset in staged_assets]},
        output={
            "initial_candidate_asset_ids": sorted(candidates_by_id),
            "initial_candidate_compositions": [
                {
                    "candidate_id": candidate.candidate_id,
                    "root_asset_ids": list(candidate.root_asset_ids),
                    "dependency_asset_ids": list(candidate.dependency_asset_ids),
                    "asset_ids": list(candidate.asset_ids),
                }
                for candidate in candidate_compositions
            ],
            "deduplicated_asset_ids": partition.deduplicated_asset_ids,
            "unsupported_initial_asset_ids": sorted(rejected_ids),
        },
        summary={
            "initial_candidate_count": len(candidates),
            "unsupported_initial_count": len(rejected_ids),
            "working_ontology_fingerprint": ontology_fingerprint(working),
        },
    )

    if not candidates:
        current = replay("initial_current_working_replay", working)
        current_eval = current.terminal_evaluation
        events.append(_replay_event(current, ontology="accepted_working"))
        return _initial_result(
            working=working,
            evaluation=current_eval,
            events=events,
            accepted_ids=accepted_ids,
            rejected_ids=rejected_ids,
            deduplicated_ids=partition.deduplicated_asset_ids,
            requires_repair=(
                current_eval.get("evaluation_available") is False
                or not bool(current_eval.get("correct"))
            ),
            failure_reason=(
                "initial_current_working_evaluation_unavailable"
                if current_eval.get("evaluation_available") is False
                else None
            ),
        )

    induction_assets = _overlay_assets(working, candidates)
    publish_working(induction_assets)
    induction = replay("initial_induction_replay", induction_assets)
    induction_eval = induction.terminal_evaluation
    events.append(
        _replay_event(
            induction,
            ontology="initial_candidates",
            stage="initial_induction",
        )
    )
    induction_query = str(induction_eval.get("query") or "").strip()
    if induction_eval.get("evaluation_available") is False or not induction_query:
        rejected_ids.update(candidates_by_id)
        publish_working(working)
        reason = (
            "initial_induction_evaluation_unavailable"
            if induction_eval.get("evaluation_available") is False
            else "initial_induction_generation_failed"
        )
        events.append(
            {
                "round": 0,
                "stage": "initial_induction_rejected",
                "evaluation": induction_eval,
                "rejected_asset_ids": sorted(candidates_by_id),
                "reason": reason,
            }
        )
        trace(
            step="initial_candidate_rejection",
            phase="initial_induction_replay",
            input={"candidate_asset_ids": sorted(candidates_by_id)},
            output={
                "rejected_asset_ids": sorted(candidates_by_id),
                "evaluation": induction_eval,
            },
            summary={
                "reason": reason,
                "failed_closed": True,
            },
        )
        return _initial_result(
            working=working,
            evaluation=induction_eval,
            events=events,
            accepted_ids=accepted_ids,
            rejected_ids=rejected_ids,
            deduplicated_ids=partition.deduplicated_asset_ids,
            requires_repair=True,
            failure_reason=reason,
        )

    selected_ids = induction.effective_selected_ontology_ids()
    raw_retrieved_ids = induction.effective_raw_retrieved_ontology_ids()
    retrieval = {asset.id: asset.id in selected_ids for asset in candidates}
    raw_retrieval = {asset.id: asset.id in raw_retrieved_ids for asset in candidates}
    assessment_trace: dict[str, Any] = {}
    try:
        assessment = assess_initial_candidates(
            provider,
            payload={
                "question": record["question"],
                "gold_sql": record["sql"],
                "evidence": record.get("evidence"),
                "gold_column_metadata": record.get("gold_column_metadata", {}),
                "current_sql": induction_eval.get("query"),
            },
            candidates=candidate_compositions,
            retrieval_by_asset=retrieval,
            raw_retrieval_by_asset=raw_retrieval,
            trace_capture=assessment_trace,
        )
        decision = build_initial_candidate_decision(
            assessment,
            candidates=candidate_compositions,
            retrieval_by_asset=retrieval,
            raw_retrieval_by_asset=raw_retrieval,
        )
    except InitialCandidateAssessmentError as exc:
        rejected_ids.update(candidates_by_id)
        publish_working(working)
        trace(
            step="initial_candidate_assessment",
            phase="initial_candidate_assessment",
            input={
                "candidate_asset_ids": sorted(candidates_by_id),
                "retrieval_by_asset": retrieval,
                "raw_retrieval_by_asset": raw_retrieval,
            },
            output={"assessment": None, "decision": None},
            prompt=assessment_trace.get("prompt"),
            response=assessment_trace.get("response"),
            summary={
                "failed_closed": True,
                "eligible_candidate_ids": [],
                "eligible_asset_ids": [],
            },
        )
        return _initial_result(
            working=working,
            evaluation=induction_eval,
            events=events,
            accepted_ids=accepted_ids,
            rejected_ids=rejected_ids,
            deduplicated_ids=partition.deduplicated_asset_ids,
            failure_reason=f"initial_candidate_assessment_invalid: {exc}",
        )

    trace(
        step="initial_candidate_assessment",
        phase="initial_candidate_assessment",
        input={
            "candidate_asset_ids": sorted(candidates_by_id),
            "retrieval_by_asset": retrieval,
            "raw_retrieval_by_asset": raw_retrieval,
        },
        output={"assessment": assessment, "decision": decision},
        prompt=assessment_trace.get("prompt"),
        response=assessment_trace.get("response"),
        summary={
            "failed_closed": False,
            "eligible_candidate_ids": decision.eligible_candidate_ids,
            "eligible_asset_ids": _composition_asset_ids(
                compositions_by_id,
                decision.eligible_candidate_ids,
            ),
        },
    )
    rejected_ids.update(_composition_asset_ids(compositions_by_id, decision.rejected_candidate_ids))
    rejected_ids.update(
        _composition_asset_ids(compositions_by_id, decision.inconclusive_candidate_ids)
    )
    eligible_asset_ids = _composition_asset_ids(
        compositions_by_id,
        decision.eligible_candidate_ids,
    )
    accepted_ids.update(eligible_asset_ids)
    visible_semantic_ids = {asset.id for asset in working} | set(eligible_asset_ids)
    eligible_assets = _remove_unavailable_rule_references(
        [candidates_by_id[asset_id] for asset_id in eligible_asset_ids],
        visible_semantic_ids=visible_semantic_ids,
    )
    working = _overlay_assets(
        working,
        eligible_assets,
    )
    publish_working(working)

    retention = replay("initial_retention_replay", working)
    retention_eval = retention.terminal_evaluation
    events.append(
        _replay_event(
            retention,
            ontology="eligible_initial_candidates",
            stage="initial_retention",
        )
    )
    retention_selected_ids = retention.effective_selected_ontology_ids()
    retention_available = retention_eval.get("evaluation_available") is not False
    trace(
        step="initial_retention_check",
        phase="initial_retention_replay",
        input={
            "eligible_asset_ids": sorted(accepted_ids),
            "selected_ontology_ids": sorted(retention_selected_ids),
        },
        output={"evaluation": _evaluation_summary(retention_eval)},
        summary={
            "accepted_asset_ids": sorted(accepted_ids),
            "evaluation_available": retention_available,
            "correct": bool(retention_eval.get("correct")),
        },
    )
    retention_failed = not retention_available or not bool(retention_eval.get("correct"))
    if retention_failed:
        reason = (
            "initial_retention_evaluation_unavailable"
            if not retention_available
            else "initial_retention_sql_incorrect"
        )
        events.append(
            {
                "round": 0,
                "stage": "initial_retention_diagnosis",
                "evaluation": retention_eval,
                "reason": reason,
            }
        )
    return _initial_result(
        working=working,
        evaluation=retention_eval,
        events=events,
        accepted_ids=accepted_ids,
        rejected_ids=rejected_ids,
        deduplicated_ids=partition.deduplicated_asset_ids,
        requires_repair=retention_failed,
        failure_reason=(
            "initial_retention_evaluation_unavailable" if not retention_available else None
        ),
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


def _remove_unavailable_rule_references(
    assets: list[NodeAsset],
    *,
    visible_semantic_ids: set[str],
) -> list[NodeAsset]:
    """Drop same-run semantic references whose candidates were not retained."""

    result: list[NodeAsset] = []
    for asset in assets:
        copied = asset.model_copy(deep=True)
        if isinstance(copied.properties, GenerationRuleProperties):
            copied.properties.references = [
                reference
                for reference in copied.properties.references
                if not reference.startswith(("concept.", "metric."))
                or reference in visible_semantic_ids
            ]
        result.append(copied)
    return result


def _composition_asset_ids(
    compositions_by_id: dict[str, Any],
    candidate_ids: list[str] | set[str],
) -> list[str]:
    return sorted(
        {
            asset_id
            for candidate_id in candidate_ids
            for asset_id in compositions_by_id[candidate_id].asset_ids
        }
    )


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
