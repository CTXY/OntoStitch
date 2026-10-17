from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.initial.compositions import (
    InitialCandidateComposition,
    _validate_compositions,
    build_initial_candidate_compositions,
)


class IntrinsicQualityDefect(NodeModel):
    asset_id: str = Field(description="Logical asset containing the defect.")
    field_or_clause: Literal[
        "name",
        "identity",
        "targets",
        "pattern",
        "expression",
        "sql_generation_plans",
        "references",
        "aggregation_policy",
        "calculation_policy",
        "triggers",
        "condition",
        "action",
        "reason",
        "ownership",
        "missing_contract",
    ]
    reason: str = Field(
        min_length=1,
        description=(
            "Intrinsic defect in the candidate asset itself; whole-case SQL errors and "
            "unrelated query behavior are not intrinsic defects."
        ),
    )


class InitialCandidateAssessmentError(ValueError):
    """Initial candidate evidence could not produce a complete trusted partition."""


class InitialCandidateAssessmentRow(NodeModel):
    candidate_id: str = Field(
        min_length=1,
        description=(
            "Exact candidate_id copied from one assessment_units entry. A Metric root or "
            "dependency asset ID is not a separate assessment row ID."
        ),
    )
    relevance_reason: str = Field(min_length=1)
    relevance_status: Literal["supported", "unsupported", "unknown"]
    usage_evidence: str = Field(min_length=1)
    usage_status: Literal["used", "not_used", "unknown"] = Field(
        description=(
            "Whether current_sql applies the Candidate-owned contract: used when all owned "
            "behavior is present, not_used when owned behavior is absent or changed, and unknown "
            "only when current_sql cannot decide. Errors outside the Candidate contract do not "
            "change this status."
        )
    )
    quality_reason: str = Field(min_length=1)
    quality_status: Literal["valid", "invalid", "inconclusive"] = Field(
        description=(
            "Intrinsic Candidate quality against Question, Gold, and its asset contract, without "
            "using retrieval or current_sql success."
        )
    )
    quality_defects: list[IntrinsicQualityDefect] = Field(default_factory=list)
    @model_validator(mode="after")
    def validate_quality_evidence(self) -> InitialCandidateAssessmentRow:
        if self.quality_status == "invalid" and not self.quality_defects:
            raise ValueError(
                "invalid initial candidate quality requires an intrinsic field-level defect"
            )
        if self.quality_status != "invalid" and self.quality_defects:
            raise ValueError(
                "intrinsic quality defects require quality_status=invalid"
            )
        return self


class InitialCandidateAssessment(NodeModel):
    candidate_assessments: list[InitialCandidateAssessmentRow] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_unique_candidates(self) -> InitialCandidateAssessment:
        candidate_ids = [row.candidate_id for row in self.candidate_assessments]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("initial assessment contains duplicate candidate rows")
        return self


class InitialCandidateDisposition(NodeModel):
    candidate_id: str = Field(min_length=1)
    disposition: Literal["eligible", "reject", "inconclusive"]
    reason: str = Field(min_length=1)


class InitialCandidateDecision(NodeModel):
    dispositions: list[InitialCandidateDisposition] = Field(default_factory=list)
    eligible_candidate_ids: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    inconclusive_candidate_ids: list[str] = Field(default_factory=list)


def extract_initial_candidate_assessment(
    llm: object,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return run_llm_extraction(
        llm=llm,
        prompt=prompt,
        output_model=InitialCandidateAssessment,
        human_payload=record,
        map_result=lambda value: [
            value.model_dump(mode="json")
            if hasattr(value, "model_dump")
            else value
        ],
        trace_capture=trace_capture,
    )


def assess_initial_candidates(
    provider: Any,
    *,
    payload: dict[str, Any],
    candidates: list[InitialCandidateComposition],
    retrieval_by_asset: dict[str, bool],
    raw_retrieval_by_asset: dict[str, bool] | None = None,
    trace_capture: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> InitialCandidateAssessment:
    _validate_compositions(candidates)
    assets_by_id = {
        asset.id: asset for candidate in candidates for asset in candidate.assets
    }
    if raw_retrieval_by_asset is None:
        raw_retrieval_by_asset = retrieval_by_asset
    if set(retrieval_by_asset) != set(assets_by_id):
        raise InitialCandidateAssessmentError(
            "retrieval evidence must classify every initial candidate"
        )
    if set(raw_retrieval_by_asset) != set(assets_by_id):
        raise InitialCandidateAssessmentError(
            "raw retrieval evidence must classify every initial candidate"
        )

    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        case_evidence = {
            key: payload[key]
            for key in (
                "question",
                "gold_sql",
                "evidence",
                "gold_column_metadata",
                "current_sql",
            )
            if key in payload
        }
        attempt_input = {
            "case_evidence": case_evidence,
            "assessment_units": [
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate": {
                        "root_asset_ids": list(candidate.root_asset_ids),
                        "dependency_asset_ids": list(candidate.dependency_asset_ids),
                        "assets": [
                            asset.model_dump(mode="json") for asset in candidate.assets
                        ],
                    },
                    "retrieval_status": _initial_retrieval_status(
                        candidate,
                        retrieval_by_asset=retrieval_by_asset,
                        raw_retrieval_by_asset=raw_retrieval_by_asset,
                    ),
                }
                for candidate in candidates
            ],
        }
        if errors:
            attempt_input["validation_feedback"] = {
                "attempt": attempt,
                "error": errors[-1],
                "instruction": _assessment_retry_instruction(
                    errors[-1],
                    expected_candidate_ids=[candidate.candidate_id for candidate in candidates],
                ),
            }
        local_trace: dict[str, Any] = {}
        try:
            rows = provider.extract_assets(
                attempt_input,
                context={
                    "prompt_name": "train_initial_candidate_assessment",
                    "prompt": load_prompt("train_initial_candidate_assessment"),
                    "trace_capture": local_trace,
                },
            )
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            assessment = InitialCandidateAssessment.model_validate(row)
            validate_initial_candidate_assessment(
                assessment,
                candidates=candidates,
                retrieval_by_asset=retrieval_by_asset,
                raw_retrieval_by_asset=raw_retrieval_by_asset,
            )
            if trace_capture is not None:
                trace_capture.update(local_trace)
                trace_capture["attempt"] = attempt
                trace_capture["validation_errors"] = list(errors)
            return assessment
        except (ValidationError, ValueError) as exc:
            errors.append(str(exc))
            if trace_capture is not None:
                trace_capture.update(local_trace)
    raise InitialCandidateAssessmentError(
        "initial candidate assessment failed after "
        f"{max_attempts} attempts: {errors[-1]}"
    )


def _initial_retrieval_status(
    candidate: InitialCandidateComposition,
    *,
    retrieval_by_asset: dict[str, bool],
    raw_retrieval_by_asset: dict[str, bool],
) -> Literal["not_retrieved", "not_selected", "selected"]:
    if not all(raw_retrieval_by_asset[item] for item in candidate.root_asset_ids):
        return "not_retrieved"
    if not all(retrieval_by_asset[item] for item in candidate.root_asset_ids):
        return "not_selected"
    return "selected"


def validate_initial_candidate_assessment(
    assessment: InitialCandidateAssessment,
    *,
    candidates: list[InitialCandidateComposition],
    retrieval_by_asset: dict[str, bool],
    raw_retrieval_by_asset: dict[str, bool] | None = None,
) -> None:
    candidate_by_id = _validate_compositions(candidates)
    asset_ids = {asset.id for candidate in candidates for asset in candidate.assets}
    expected_ids = set(candidate_by_id)
    actual_ids = {row.candidate_id for row in assessment.candidate_assessments}
    if actual_ids != expected_ids:
        raise ValueError(
            "initial assessment candidate partition mismatch: "
            f"missing={sorted(expected_ids-actual_ids)}, "
            f"unknown={sorted(actual_ids-expected_ids)}"
        )
    if set(retrieval_by_asset) != asset_ids:
        raise ValueError(
            "retrieval evidence must classify every initial candidate"
        )
    if raw_retrieval_by_asset is None:
        raw_retrieval_by_asset = retrieval_by_asset
    if set(raw_retrieval_by_asset) != asset_ids:
        raise ValueError(
            "raw retrieval evidence must classify every initial candidate"
        )
    if any(
        retrieval_by_asset[asset_id] and not raw_retrieval_by_asset[asset_id]
        for asset_id in asset_ids
    ):
        raise ValueError("selected initial candidate must also be raw retrieved")
    selected_by_candidate = {
        candidate.candidate_id: all(
            retrieval_by_asset[item] for item in candidate.root_asset_ids
        )
        for candidate in candidates
    }
    for row in assessment.candidate_assessments:
        if (
            not selected_by_candidate[row.candidate_id]
            and row.usage_status == "used"
        ):
            raise ValueError(
                "non-selected initial candidate cannot be classified as used: "
                f"{row.candidate_id}"
            )
        composition_asset_ids = set(candidate_by_id[row.candidate_id].asset_ids)
        unknown_defect_assets = sorted(
            {
                defect.asset_id
                for defect in row.quality_defects
                if defect.asset_id is not None
                and defect.asset_id not in composition_asset_ids
            }
        )
        if unknown_defect_assets:
            raise ValueError(
                "initial candidate quality defect references assets outside its composition: "
                f"{unknown_defect_assets}"
            )
        if len(composition_asset_ids) > 1 and any(
            defect.asset_id is None for defect in row.quality_defects
        ):
            raise ValueError("multi-asset candidate quality defects require asset_id")
    _validate_same_name_concept_metric_eligibility(
        assessment,
        candidates=candidates,
        retrieval_by_asset=retrieval_by_asset,
    )


def _validate_same_name_concept_metric_eligibility(
    assessment: InitialCandidateAssessment,
    *,
    candidates: list[InitialCandidateComposition],
    retrieval_by_asset: dict[str, bool],
) -> None:
    """Reject an assessment that accepts competing same-name owner definitions."""

    rows_by_id = {
        row.candidate_id: row for row in assessment.candidate_assessments
    }
    eligible_claims: dict[str, dict[NodeType, set[str]]] = {}
    for candidate in candidates:
        row = rows_by_id[candidate.candidate_id]
        if not (
            row.relevance_status == "supported"
            and row.usage_status == "used"
            and row.quality_status == "valid"
            and all(retrieval_by_asset[item] for item in candidate.root_asset_ids)
        ):
            continue
        roots = set(candidate.root_asset_ids)
        for asset in candidate.assets:
            if asset.id not in roots or asset.node_type not in {
                NodeType.SEMANTIC_MAPPING,
                NodeType.METRIC,
            }:
                continue
            normalized_name = " ".join(asset.name.split()).casefold()
            if not normalized_name:
                continue
            eligible_claims.setdefault(normalized_name, {}).setdefault(
                asset.node_type, set()
            ).add(candidate.candidate_id)

    conflicts = {
        name: sorted(
            claims.get(NodeType.SEMANTIC_MAPPING, set())
            | claims.get(NodeType.METRIC, set())
        )
        for name, claims in eligible_claims.items()
        if claims.get(NodeType.SEMANTIC_MAPPING) and claims.get(NodeType.METRIC)
    }
    if conflicts:
        raise ValueError(
            "same-name Concept/Metric conflict cannot have more than one eligible "
            f"definition: {conflicts}"
        )


def build_initial_candidate_decision(
    assessment: InitialCandidateAssessment,
    *,
    candidates: list[InitialCandidateComposition],
    retrieval_by_asset: dict[str, bool],
    raw_retrieval_by_asset: dict[str, bool] | None = None,
) -> InitialCandidateDecision:
    if raw_retrieval_by_asset is None:
        raw_retrieval_by_asset = retrieval_by_asset
    validate_initial_candidate_assessment(
        assessment,
        candidates=candidates,
        retrieval_by_asset=retrieval_by_asset,
        raw_retrieval_by_asset=raw_retrieval_by_asset,
    )
    dispositions: list[InitialCandidateDisposition] = []
    candidates_by_id = _validate_compositions(candidates)
    for row in assessment.candidate_assessments:
        composition = candidates_by_id[row.candidate_id]
        retrieved = all(
            retrieval_by_asset[item] for item in composition.root_asset_ids
        )
        raw_retrieved = all(
            raw_retrieval_by_asset[item]
            for item in composition.root_asset_ids
        )
        if (
            row.relevance_status == "unsupported"
            or row.quality_status == "invalid"
            or not retrieved
            or row.usage_status == "not_used"
        ):
            disposition = "reject"
            reason = _rejection_reason(
                row,
                raw_retrieved=raw_retrieved,
                selected=retrieved,
            )
        elif (
            row.relevance_status == "supported"
            and row.quality_status == "valid"
            and row.usage_status == "used"
        ):
            disposition = "eligible"
            reason = "supported_valid_retrieved_and_used"
        else:
            disposition = "inconclusive"
            reason = "initial_candidate_evidence_incomplete_or_unknown"
        dispositions.append(
            InitialCandidateDisposition(
                candidate_id=row.candidate_id,
                disposition=disposition,
                reason=reason,
            )
        )

    return InitialCandidateDecision(
        dispositions=dispositions,
        eligible_candidate_ids=[
            row.candidate_id
            for row in dispositions
            if row.disposition == "eligible"
        ],
        rejected_candidate_ids=[
            row.candidate_id
            for row in dispositions
            if row.disposition == "reject"
        ],
        inconclusive_candidate_ids=[
            row.candidate_id
            for row in dispositions
            if row.disposition == "inconclusive"
        ],
    )


def _assessment_retry_instruction(
    error: str,
    *,
    expected_candidate_ids: list[str],
) -> str:
    base = (
        "Classify every expected initial candidate exactly once. Preserve candidate IDs and "
        "return only relevance, usage, and intrinsic quality evidence; do not return an "
        "action or modification task."
    )
    if "candidate partition mismatch" in error:
        return (
            f"{base} Return exactly one row for each of these assessment unit IDs: "
            f"{expected_candidate_ids}. Do not create rows for Metric root or dependency asset "
            "IDs. Put a member defect in the owning unit row's quality_defects.asset_id."
        )
    if "intrinsic field-level defect" in error:
        return (
            f"{base} For quality_status=invalid, add quality_defects naming the exact "
            "candidate field or ownership defect. Do not use a pre-existing whole-case SQL "
            "error, an unrelated residual error, or missing query-use behavior as an "
            "intrinsic asset defect."
        )
    if "quality_status=invalid" in error:
        return f"{base} Remove quality_defects unless quality_status is invalid."
    if "quality defects require asset_id" in error:
        return (
            f"{base} For a multi-asset Metric candidate, identify the exact composition "
            "asset containing every quality defect."
        )
    if "same-name Concept/Metric conflict" in error:
        return (
            f"{base} For each same-name Concept/Metric conflict, compare both complete "
            "definitions with the same atomic Question term and its complete Gold "
            "implementation. At most one may be supported, used, and valid. Mark an "
            "operand-only or wrong-owner Candidate invalid with an ownership defect; if "
            "the evidence cannot choose, do not classify both as eligible."
        )
    return base


def _rejection_reason(
    row: InitialCandidateAssessmentRow,
    *,
    raw_retrieved: bool,
    selected: bool,
) -> str:
    if row.relevance_status == "unsupported":
        return "unsupported_relevance"
    if row.quality_status == "invalid":
        return "invalid_content"
    if not raw_retrieved:
        return "not_retrieved"
    if not selected:
        return "not_selected"
    if row.usage_status == "not_used":
        return "not_used"
    return "rejected"
