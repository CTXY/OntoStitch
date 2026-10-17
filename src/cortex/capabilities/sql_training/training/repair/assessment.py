from __future__ import annotations

from collections import defaultdict
import re
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from pydantic import Field, ValidationError, model_validator

from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction
from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.repair.contracts import OptimizationOwner
from cortex.capabilities.sql_training.training.repair.plan import (
    FrozenCandidateBindings,
    HeavyRoundPlan,
)


class TargetAssessment(NodeModel):
    target_id: str = Field(
        min_length=1,
        description="Exact target_id from an assessment unit's owned_targets.",
    )
    status: Literal["resolved", "unresolved", "inconclusive"] = Field(
        description=(
            "Resolution of the frozen target contract in current SQL. Overall result correctness "
            "does not resolve SQL that differs from that contract."
        )
    )
    evidence: str = Field(
        min_length=1,
        description="Current-SQL evidence for this Target status.",
    )


class ResidualQualityDefect(NodeModel):
    asset_id: str = Field(min_length=1)
    field_or_clause: str = Field(
        min_length=1,
        pattern=(
            r"^(?:properties\.)?(?:name|description|aliases|targets|pattern|expression|"
            r"sql_generation_plans|references|aggregation_policy|calculation_policy|triggers|"
            r"condition|action|reason|operation_intent)(?:\..+)?$"
        ),
        description=(
            "Exact Candidate field or nested field path containing the defect. A missing value "
            "is assigned to the field that should contain it."
        ),
    )
    reason: str = Field(
        min_length=1,
        description=(
            "Intrinsic defect proved by Candidate content versus the asset contract, frozen "
            "requirement, or Gold. Do not infer a defect from current SQL application."
        ),
    )


class CandidateAssessment(NodeModel):
    candidate_id: str = Field(
        min_length=1,
        description="Exact candidate_id from one assessment_units entry.",
    )
    application: Literal[
        "not_retrieved",
        "not_selected",
        "not_applied",
        "partially_applied",
        "applied",
        "inconclusive",
    ] = Field(
        description=(
            "Observed use of the Candidate in the current trial. Application is independent of "
            "intrinsic asset quality."
        )
    )
    quality_status: Literal["valid", "invalid", "inconclusive"] = Field(
        description=(
            "Intrinsic quality from Candidate content versus the canonical asset contract, frozen "
            "requirement, and Gold. Current SQL is application evidence only: its non-use, partial "
            "use, or different implementation does not make the Candidate invalid. Canonical "
            "owner boundaries override the frozen owner: a Generation Rule that repeats an "
            "accepted Working Metric formula of the same identity is invalid. An overlapping "
            "unaccepted Candidate does not establish ownership."
        )
    )
    quality_reason: str = Field(
        min_length=1,
        description=(
            "Why the Candidate itself does or does not satisfy the asset contract, frozen "
            "requirement, and Gold. Current SQL is not intrinsic-quality evidence."
        ),
    )
    quality_defects: list[ResidualQualityDefect] = Field(default_factory=list)
    problem: Literal[
        "none",
        "retrieval",
        "content",
        "application_policy",
        "harmful",
        "inconclusive",
    ] = Field(
        description=(
            "Refinement route. application_policy is reserved for a valid, retrieved, selected "
            "Concept or Metric when current SQL uses an evidenced non-equivalent alternative "
            "instead."
        )
    )
    evidence: str = Field(
        min_length=1,
        description=(
            "Evidence for application in the current trial. Quality evidence belongs in "
            "quality_reason and quality_defects."
        ),
    )
    modification_instruction: str = Field(
        default="",
        description=(
            "Short diagnostic pointer for this same Candidate, not a repair plan or replacement "
            "asset text. Name one insufficient field for application-only failure, or the minimum "
            "fields proved defective or harmful. Identify the semantic gap and Gold-consistent "
            "behavior to preserve without prescribing final field content. Detailed proof and any "
            "observed competitor belong in evidence or quality_defects. For application_policy, "
            "summarize only the missing choice between the complete Concept/Metric and the "
            "proved alternative; do not name an anchor asset field. Otherwise use one short sentence: "
            "'<field path>: <semantic gap>; preserve <owned behavior>.' Never prescribe an edit, "
            "draft an action or SQL template, create a sibling asset, reroute the owner, or return "
            "no change for unresolved application. Do not copy example values; describe their "
            "semantic source instead."
        ),
    )
    regressed_target_ids: tuple[str, ...] = ()
    regression_evidence: str = ""

    @model_validator(mode="after")
    def validate_feedback(self) -> CandidateAssessment:
        if self.quality_status == "invalid" and not self.quality_defects:
            raise ValueError("invalid residual candidate quality requires quality_defects")
        if self.quality_status != "invalid" and self.quality_defects:
            raise ValueError("residual quality_defects require quality_status=invalid")
        if self.quality_status == "invalid" and self.problem not in {"content", "harmful"}:
            raise ValueError("invalid residual candidate quality requires content or harmful")
        if self.problem == "none" and self.quality_status != "valid":
            raise ValueError("problem=none requires quality_status=valid")
        unresolved_applications = {
            "not_retrieved",
            "not_selected",
            "not_applied",
            "partially_applied",
        }
        if (
            self.application in unresolved_applications
            and not self.modification_instruction.strip()
        ):
            raise ValueError(
                f"{self.application} application requires modification_instruction"
            )
        if (
            self.application == "not_retrieved"
            and self.quality_status != "invalid"
            and self.problem != "retrieval"
        ):
            raise ValueError(
                "not_retrieved application with non-invalid quality requires problem=retrieval"
            )
        if self.application == "not_selected" and self.problem != "content":
            raise ValueError("not_selected application requires problem=content")
        if self.application in {"not_applied", "partially_applied"} and self.problem not in {
            "content",
            "application_policy",
        }:
            raise ValueError(
                f"{self.application} application requires problem=content/application_policy"
            )
        if self.problem == "application_policy" and (
            self.quality_status != "valid"
            or self.application not in {"not_applied", "partially_applied"}
        ):
            raise ValueError(
                "application_policy requires valid quality and an application failure"
            )
        if self.problem == "none" and self.application != "applied":
            raise ValueError("problem=none requires application=applied")
        if self.problem in {"retrieval", "content", "application_policy"} and not (
            self.modification_instruction.strip()
        ):
            raise ValueError(f"{self.problem} problem requires modification_instruction")
        if self.problem == "harmful" and not self.modification_instruction.strip():
            raise ValueError("harmful problem requires concrete modification_instruction")
        if self.regressed_target_ids and not self.regression_evidence.strip():
            raise ValueError("regressed targets require regression_evidence")
        if len(self.regressed_target_ids) != len(set(self.regressed_target_ids)):
            raise ValueError("candidate assessment contains duplicate regressed target IDs")
        if self.regressed_target_ids and self.problem != "harmful":
            raise ValueError("regressed targets require problem=harmful")
        return self


class JointIterationDecision(NodeModel):
    accept_candidate_ids: tuple[str, ...] = ()
    hold_candidate_ids: tuple[str, ...] = ()
    refine_candidate_ids: tuple[str, ...] = ()
    regression_culprit_candidate_ids: tuple[str, ...] = ()
    close_reason: str | None = None


class JointModificationTask(NodeModel):
    """Owner-optimizer feedback DTO; it is not a pipeline decision or planner output."""

    owner: OptimizationOwner
    asset_id: str
    fields_to_modify: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)
    modification_instruction: str = Field(min_length=1)


class ResidualJointAssessment(NodeModel):
    target_assessments: list[TargetAssessment]
    candidate_assessments: list[CandidateAssessment]


class ResidualJointAssessmentError(ValueError):
    """The residual assessment LLM could not satisfy the frozen trial contract."""


_FIELD_POINTER_RE = re.compile(
    r"^\s*(?:properties\.)?(?:name|description|aliases|targets|pattern|expression|"
    r"sql_generation_plans|references|aggregation_policy|calculation_policy|triggers|"
    r"condition|action|reason|operation_intent)(?:[.\s:]|$)",
    re.IGNORECASE,
)

_METRIC_RULE_FIELD_RE = re.compile(
    r"\b(?:properties\.)?(?:triggers|condition|action|reason|operation_intent)\b|"
    r"\bquestion\.phrases\b|\bsql\s+triggers?\b",
    re.IGNORECASE,
)


def extract_residual_joint_assessment(
    llm: object,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return run_llm_extraction(
        llm=llm,
        prompt=prompt,
        output_model=ResidualJointAssessment,
        human_payload=record,
        map_result=lambda parsed: [
            ResidualJointAssessment.model_validate(parsed).model_dump(mode="json")
        ],
        trace_capture=trace_capture,
        allow_output_fixing=False,
    )


def assess_residual_joint_trial(
    provider: Any,
    *,
    payload: dict[str, Any],
    plan: HeavyRoundPlan,
    bindings: FrozenCandidateBindings,
    reviewed_candidate_ids: set[str],
    accepted_candidate_ids: set[str] | None = None,
    accepted_working_assets: list[dict[str, Any]] | None = None,
    trace_capture: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> ResidualJointAssessment:
    expected_target_ids = {target.target_id for target in plan.targets}
    retrieved_asset_ids = (
        set(payload.get("retrieved_logical_asset_ids") or [])
        if "retrieved_logical_asset_ids" in payload
        else None
    )
    raw_retrieved_asset_ids = (
        set(payload.get("raw_retrieved_asset_ids") or [])
        if "raw_retrieved_asset_ids" in payload
        else None
    )
    if reviewed_candidate_ids != set(bindings.candidates_by_id):
        raise ValueError("reviewed candidates must equal active plus accepted frozen candidates")
    candidate_asset_ids_by_candidate = _candidate_asset_ids(
        payload.get("candidate_assets")
    )
    assessment_input = _assessment_input_view(
        payload,
        plan=plan,
        bindings=bindings,
        reviewed_candidate_ids=reviewed_candidate_ids,
        accepted_candidate_ids=set(accepted_candidate_ids or set()),
        accepted_working_assets=accepted_working_assets or [],
    )
    errors: list[dict[str, Any]] = []
    for attempt in range(1, max_attempts + 1):
        attempt_input = dict(assessment_input)
        if errors:
            attempt_input["validation_feedback"] = {
                "attempt": attempt,
                "error": errors[-1],
                "instruction": (
                    "Correct only the reported output-contract error. Cover every unique owned "
                    "Target and every assessment unit exactly once. Preserve all other decisions."
                ),
            }
        local_trace: dict[str, Any] = {}
        try:
            rows = provider.extract_assets(
                attempt_input,
                context={
                    "prompt_name": "train_residual_joint_assessment",
                    "prompt": load_prompt("train_residual_joint_assessment"),
                    "trace_capture": local_trace,
                },
            )
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            raw = row.get("residual_joint_assessment", row)
            assessment = ResidualJointAssessment.model_validate(raw)
            _validate_residual_assessment(
                assessment,
                expected_target_ids=expected_target_ids,
                expected_candidate_ids=reviewed_candidate_ids,
                retrieved_asset_ids=retrieved_asset_ids,
                raw_retrieved_asset_ids=raw_retrieved_asset_ids,
                logical_asset_id_by_candidate={
                    candidate_id: bindings.candidates_by_id[candidate_id].logical_asset_id
                    for candidate_id in reviewed_candidate_ids
                },
                owner_by_candidate={
                    candidate_id: bindings.candidates_by_id[candidate_id].owner
                    for candidate_id in reviewed_candidate_ids
                },
                candidate_asset_ids_by_candidate=candidate_asset_ids_by_candidate,
                owned_target_ids_by_candidate={
                    candidate_id: bindings.candidates_by_id[candidate_id].owned_target_ids
                    for candidate_id in reviewed_candidate_ids
                },
            )
            if trace_capture is not None:
                trace_capture.update(local_trace)
                trace_capture["attempt"] = attempt
                trace_capture["validation_errors"] = list(errors)
            if _contains_inconclusive(assessment):
                raise ValueError(
                    "inconclusive target or candidate assessment requires a bounded retry"
                )
            return assessment
        except (OutputParserException, ValidationError, ValueError) as exc:
            errors.append(
                {
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            if trace_capture is not None:
                trace_capture.update(local_trace)
    raise ResidualJointAssessmentError(
        f"residual joint assessment failed after {max_attempts} attempts: {errors[-1]}"
    )


def _validate_residual_assessment(
    assessment: ResidualJointAssessment,
    *,
    expected_target_ids: set[str],
    expected_candidate_ids: set[str],
    retrieved_asset_ids: set[str] | None,
    raw_retrieved_asset_ids: set[str] | None,
    logical_asset_id_by_candidate: dict[str, str],
    owner_by_candidate: dict[str, OptimizationOwner],
    candidate_asset_ids_by_candidate: dict[str, tuple[str, ...]],
    owned_target_ids_by_candidate: dict[str, tuple[str, ...]],
) -> None:
    target_ids = [item.target_id for item in assessment.target_assessments]
    candidate_ids = [item.candidate_id for item in assessment.candidate_assessments]
    if len(target_ids) != len(set(target_ids)) or set(target_ids) != expected_target_ids:
        raise ValueError("target assessments must exactly cover frozen targets")
    if (
        len(candidate_ids) != len(set(candidate_ids))
        or set(candidate_ids) != expected_candidate_ids
    ):
        raise ValueError("candidate assessments must exactly cover reviewed candidates")
    unknown_regressions = sorted(
        {
            target_id
            for item in assessment.candidate_assessments
            for target_id in item.regressed_target_ids
            if target_id not in expected_target_ids
        }
    )
    if unknown_regressions:
        raise ValueError(f"regression references unknown frozen targets: {unknown_regressions}")
    target_status_by_id = {
        item.target_id: item.status for item in assessment.target_assessments
    }
    for item in assessment.candidate_assessments:
        owner = owner_by_candidate[item.candidate_id]
        if owner == OptimizationOwner.METRIC:
            for defect in item.quality_defects:
                if _METRIC_RULE_FIELD_RE.search(defect.field_or_clause):
                    raise ValueError(
                        "Metric residual assessment cannot target Generation Rule fields: "
                        f"{defect.field_or_clause}"
                    )
            if _METRIC_RULE_FIELD_RE.search(item.modification_instruction):
                raise ValueError(
                    "Metric residual assessment modification_instruction cannot target "
                    "Generation Rule fields"
                )
        if item.problem == "application_policy" and owner_by_candidate[item.candidate_id] not in {
            OptimizationOwner.SEMANTIC_MAPPING,
            OptimizationOwner.METRIC,
        }:
            raise ValueError(
                "application_policy is supported only for Concept or Metric candidates"
            )
        if (
            item.problem == "application_policy"
            and _FIELD_POINTER_RE.search(item.modification_instruction)
        ):
            raise ValueError(
                "application_policy must describe the missing selection policy, not a "
                "Concept or Metric field modification"
            )
        member_ids = set(candidate_asset_ids_by_candidate.get(item.candidate_id, ()))
        unknown_defect_assets = sorted(
            defect.asset_id for defect in item.quality_defects if defect.asset_id not in member_ids
        )
        if unknown_defect_assets:
            raise ValueError(
                "residual quality defect references assets outside its candidate: "
                f"{unknown_defect_assets}"
            )
        owned_target_ids = owned_target_ids_by_candidate[item.candidate_id]
        if item.application == "applied" and any(
            target_status_by_id[target_id] == "unresolved" for target_id in owned_target_ids
        ):
            if not (
                item.problem == "content"
                and item.modification_instruction.strip()
            ):
                raise ValueError(
                    f"candidate {item.candidate_id} has an unresolved owned target and must "
                    "return problem=content with a modification instruction"
                )
    if retrieved_asset_ids is None or raw_retrieved_asset_ids is None:
        return
    for item in assessment.candidate_assessments:
        asset_id = logical_asset_id_by_candidate[item.candidate_id]
        was_raw_retrieved = asset_id in raw_retrieved_asset_ids
        was_selected = asset_id in retrieved_asset_ids
        expected_application = (
            "not_retrieved"
            if not was_raw_retrieved
            else "not_selected"
            if not was_selected
            else None
        )
        expected_problems = (
            {"retrieval", "content", "harmful"}
            if expected_application == "not_retrieved" and item.quality_status == "invalid"
            else {"retrieval"}
            if expected_application == "not_retrieved"
            else {"content"}
        )
        if expected_application and not (
            item.application == expected_application and item.problem in expected_problems
        ):
            expected_problem_label = "/".join(sorted(expected_problems))
            raise ValueError(
                f"candidate {item.candidate_id} asset {asset_id} must be classified as "
                f"{expected_application}/{expected_problem_label} from authoritative "
                "stage evidence and intrinsic quality"
            )
        if expected_application is None and item.application in {"not_retrieved", "not_selected"}:
            raise ValueError(
                f"candidate {item.candidate_id} asset {asset_id} was selected; "
                "it cannot be classified as a retrieval-stage failure"
            )
        if item.problem == "application_policy" and expected_application is not None:
            raise ValueError(
                "application_policy requires authoritative raw retrieval and selection"
            )


def _candidate_asset_ids(candidate_assets: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(candidate_assets, list):
        return {}
    candidate_asset_ids: dict[str, tuple[str, ...]] = {}
    for row in candidate_assets:
        if not isinstance(row, dict) or not isinstance(row.get("asset"), dict):
            continue
        candidate_id = str(row.get("candidate_id") or "")
        asset = row["asset"]
        dependency_assets = row.get("dependency_assets")
        if not isinstance(dependency_assets, list):
            dependency_assets = []
        member_ids = (
            str(asset.get("id") or ""),
            *(
                str(dependency.get("id") or "")
                for dependency in dependency_assets
                if isinstance(dependency, dict)
            ),
        )
        candidate_asset_ids[candidate_id] = tuple(
            member_id for member_id in member_ids if member_id
        )
    return candidate_asset_ids


def _assessment_input_view(
    payload: dict[str, Any],
    *,
    plan: HeavyRoundPlan,
    bindings: FrozenCandidateBindings,
    reviewed_candidate_ids: set[str],
    accepted_candidate_ids: set[str],
    accepted_working_assets: list[dict[str, Any]],
) -> dict[str, Any]:
    candidates = {
        str(row.get("candidate_id") or ""): row
        for row in payload.get("candidate_assets") or []
        if isinstance(row, dict)
    }
    stage_facts = {
        str(row.get("candidate_id") or ""): {
            "raw_retrieved": bool(row.get("raw_retrieved")),
            "selected": bool(row.get("selected")),
        }
        for row in payload.get("candidate_stage_evidence") or []
        if isinstance(row, dict)
    }
    raw_retrieved_ids = set(payload.get("raw_retrieved_asset_ids") or [])
    selected_ids = set(payload.get("retrieved_logical_asset_ids") or [])
    has_legacy_stage_facts = (
        "raw_retrieved_asset_ids" in payload and "retrieved_logical_asset_ids" in payload
    )
    targets = {target.target_id: target for target in plan.targets}
    omitted_keys = {
        "candidate_assets",
        "candidate_stage_evidence",
        "candidate_contract_facts",
        "frozen_targets",
        "reviewed_candidates",
        "retrieved_logical_asset_ids",
        "raw_retrieved_asset_ids",
    }
    result = {key: value for key, value in payload.items() if key not in omitted_keys}
    result["accepted_working_assets"] = accepted_working_assets
    result["assessment_units"] = [
        {
            "candidate_id": candidate_id,
            "candidate_state": (
                "accepted_in_round" if candidate_id in accepted_candidate_ids else "active"
            ),
            "candidate": candidates.get(candidate_id),
            "owned_targets": [
                targets[target_id].model_dump(mode="json")
                for target_id in bindings.candidates_by_id[candidate_id].owned_target_ids
            ],
            "stage_facts": stage_facts.get(
                candidate_id,
                {
                    "raw_retrieved": (
                        bindings.candidates_by_id[candidate_id].logical_asset_id
                        in raw_retrieved_ids
                        if has_legacy_stage_facts
                        else None
                    ),
                    "selected": (
                        bindings.candidates_by_id[candidate_id].logical_asset_id in selected_ids
                        if has_legacy_stage_facts
                        else None
                    ),
                },
            ),
        }
        for candidate_id in sorted(reviewed_candidate_ids)
    ]
    return result


def _contains_inconclusive(assessment: ResidualJointAssessment) -> bool:
    return any(item.status == "inconclusive" for item in assessment.target_assessments) or any(
        item.application == "inconclusive"
        or item.quality_status == "inconclusive"
        or item.problem == "inconclusive"
        for item in assessment.candidate_assessments
    )


def decide_joint_iteration(
    *,
    plan: HeavyRoundPlan,
    bindings: FrozenCandidateBindings,
    target_assessments: list[TargetAssessment],
    candidate_assessments: list[CandidateAssessment],
    accepted_candidate_ids: set[str] | None = None,
) -> JointIterationDecision:
    accepted = set(accepted_candidate_ids or set())
    target_by_id = _exact_rows(
        target_assessments,
        expected={item.target_id for item in plan.targets},
        key=lambda item: item.target_id,
        label="target assessment",
    )
    candidate_by_id = _exact_rows(
        candidate_assessments,
        expected=set(bindings.candidates_by_id),
        key=lambda item: item.candidate_id,
        label="candidate assessment",
    )
    unknown_regressions = {
        target_id
        for assessment in candidate_assessments
        for target_id in assessment.regressed_target_ids
        if target_id not in target_by_id
    }
    if unknown_regressions:
        raise ValueError(f"regression references unknown targets: {sorted(unknown_regressions)}")
    if not accepted <= set(bindings.candidates_by_id):
        raise ValueError("accepted candidates must exist in frozen bindings")

    reopened_targets = {
        target_id
        for candidate_id in accepted
        for target_id in bindings.candidates_by_id[candidate_id].owned_target_ids
        if target_by_id[target_id].status == "unresolved"
    }
    if reopened_targets:
        for candidate_id in accepted:
            if candidate_by_id[candidate_id].problem == "harmful":
                return JointIterationDecision(close_reason="accepted_working_asset_harmful")
        culprits = {
            assessment.candidate_id
            for assessment in candidate_assessments
            if assessment.candidate_id not in accepted
            and reopened_targets.intersection(assessment.regressed_target_ids)
        }
        if len(culprits) != 1:
            return JointIterationDecision(
                close_reason="accepted_target_regression_attribution_inconclusive"
            )
        culprit = next(iter(culprits))
        if not candidate_by_id[culprit].modification_instruction.strip():
            return JointIterationDecision(
                close_reason="regression_culprit_has_no_safe_refinement"
            )
        return JointIterationDecision(
            refine_candidate_ids=(culprit,),
            regression_culprit_candidate_ids=(culprit,),
        )

    if any(item.status == "inconclusive" for item in target_assessments):
        return JointIterationDecision(close_reason="target_assessment_inconclusive")
    if any(
        item.problem == "inconclusive" or item.quality_status == "inconclusive"
        for item in candidate_assessments
    ):
        return JointIterationDecision(close_reason="candidate_assessment_inconclusive")

    active = set(bindings.candidates_by_id) - accepted
    accepts: set[str] = set()
    holds: set[str] = set()
    refines: set[str] = set()
    for candidate_id in active:
        binding = bindings.candidates_by_id[candidate_id]
        assessment = candidate_by_id[candidate_id]
        statuses = [target_by_id[item].status for item in binding.owned_target_ids]
        if (
            all(status == "resolved" for status in statuses)
            and assessment.application == "applied"
            and assessment.quality_status == "valid"
            and assessment.problem == "none"
            and not assessment.regressed_target_ids
        ):
            accepts.add(candidate_id)
        elif assessment.problem == "application_policy":
            holds.add(candidate_id)
        elif (
            assessment.problem in {"retrieval", "content", "harmful"}
            or assessment.application
            in {"not_retrieved", "not_selected", "not_applied", "partially_applied"}
        ):
            refines.add(candidate_id)
        else:
            holds.add(candidate_id)

    unresolved_targets = {
        item.target_id for item in target_assessments if item.status == "unresolved"
    }
    refinable_targets = {
        target_id
        for candidate_id in refines
        for target_id in bindings.candidates_by_id[candidate_id].owned_target_ids
    }
    if unresolved_targets - refinable_targets:
        return JointIterationDecision(close_reason="unresolved_target_has_no_refinement_path")

    candidates_by_target = defaultdict(set)
    for target_id, candidate_ids in bindings.candidate_ids_by_target.items():
        candidates_by_target[target_id].update(candidate_ids)
    for target_id, neighbors in candidates_by_target.items():
        if target_by_id[target_id].status != "resolved":
            continue
        active_neighbors = neighbors - accepted
        if active_neighbors and not active_neighbors <= accepts:
            accepts.difference_update(active_neighbors)
            holds.update(active_neighbors - refines)

    return JointIterationDecision(
        accept_candidate_ids=tuple(sorted(accepts)),
        hold_candidate_ids=tuple(sorted(holds - accepts - refines)),
        refine_candidate_ids=tuple(sorted(refines)),
    )


def _exact_rows(rows, *, expected: set[str], key, label: str):
    by_id = {key(item): item for item in rows}
    if len(by_id) != len(rows):
        raise ValueError(f"duplicate {label} rows")
    if set(by_id) != expected:
        raise ValueError(
            f"{label} rows must exactly cover frozen IDs; "
            f"missing={sorted(expected - set(by_id))}, extra={sorted(set(by_id) - expected)}"
        )
    return by_id
