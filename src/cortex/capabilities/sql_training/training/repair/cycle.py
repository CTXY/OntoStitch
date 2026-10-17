from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.repair.assessment import (
    CandidateAssessment,
    ResidualJointAssessment,
    decide_joint_iteration,
)
from cortex.capabilities.sql_training.training.repair.contracts import (
    OptimizationOwner,
    asset_fingerprint,
)
from cortex.capabilities.sql_training.training.repair.plan import (
    CandidateAsset,
    CandidateInitializationResult,
    HeavyRoundPlan,
    InitializationGroup,
    add_application_policy_companion,
    build_initialization_groups,
    freeze_candidate_bindings,
    validate_initialization_result,
)
from cortex.capabilities.sql_training.training.repair.trial import (
    CaseWorkingOntology,
    accept_residual_candidates,
    materialize_residual_trial,
)


class CandidateDisposition(NodeModel):
    round_number: int = 0
    candidate_id: str
    logical_asset_id: str
    disposition: str
    reason: str = ""


class AcceptanceEvidence(NodeModel):
    round_number: int
    candidate_id: str
    logical_asset_id: str
    composition_asset_ids: tuple[str, ...]
    owned_target_ids: tuple[str, ...]
    resolved_target_ids: tuple[str, ...]
    application: str
    evidence: str
    regressed_target_ids: tuple[str, ...] = ()


class RepairCycleResult(NodeModel):
    evaluation: dict[str, Any]
    working: CaseWorkingOntology
    rounds_run: int
    trials_run: int
    completed: bool = False
    failure_reason: str | None = None
    candidate_dispositions: tuple[CandidateDisposition, ...] = ()
    acceptance_evidence: tuple[AcceptanceEvidence, ...] = ()
    remaining_target_ids: tuple[str, ...] = ()


AnalyzeErrors = Callable[[dict[str, Any], CaseWorkingOntology, int], list[dict[str, Any]]]
BuildPlan = Callable[
    [list[dict[str, Any]], dict[str, Any], CaseWorkingOntology, int], HeavyRoundPlan
]
Initialize = Callable[
    [InitializationGroup, HeavyRoundPlan, CaseWorkingOntology, CaseWorkingOntology],
    CandidateInitializationResult,
]
ExecuteTrial = Callable[[Any, int, int], dict[str, Any]]
ExecuteFinal = Callable[[Any, int], dict[str, Any]]
AssessTrial = Callable[
    [dict[str, Any], HeavyRoundPlan, Any, dict[str, CandidateAsset], set[str]],
    ResidualJointAssessment,
]
RefineCandidate = Callable[[CandidateAsset, Any, list[Any], HeavyRoundPlan], CandidateAsset]
WorkingUpdated = Callable[[CaseWorkingOntology], None]


class RepairCycle:
    """Run repair with only accepted Working and the last evaluation across rounds."""

    def __init__(
        self,
        *,
        analyze_errors: AnalyzeErrors,
        build_plan: BuildPlan,
        initialize: Initialize,
        execute_trial: ExecuteTrial,
        execute_final: ExecuteFinal | None = None,
        assess_trial: AssessTrial,
        refine_candidate: RefineCandidate,
        on_working_updated: WorkingUpdated | None = None,
    ):
        self.analyze_errors = analyze_errors
        self.build_plan = build_plan
        self.initialize = initialize
        self.execute_trial = execute_trial
        self.execute_final = execute_final
        self.assess_trial = assess_trial
        self.refine_candidate = refine_candidate
        self.on_working_updated = on_working_updated

    def run(
        self,
        *,
        initial_evaluation: dict[str, Any],
        working: CaseWorkingOntology,
        max_rounds: int,
        max_iterations: int,
    ) -> RepairCycleResult:
        diagnostic = dict(initial_evaluation)
        trials_run = 0
        dispositions: list[CandidateDisposition] = []
        acceptance: list[AcceptanceEvidence] = []
        remaining_targets: set[str] = set()
        if diagnostic.get("correct"):
            return _result(diagnostic, working, 0, 0, True, None, dispositions, acceptance)

        for round_number in range(1, max_rounds + 1):
            if _generation_failed(diagnostic):
                return _result(
                    diagnostic,
                    working,
                    round_number - 1,
                    trials_run,
                    False,
                    "candidate_generation_failed",
                    dispositions,
                    acceptance,
                    remaining_targets,
                )
            repair_groups = self.analyze_errors(
                diagnostic, working.model_copy(deep=True), round_number
            )
            if not repair_groups:
                return _result(
                    diagnostic,
                    working,
                    round_number,
                    trials_run,
                    False,
                    "error_analysis_failed",
                    dispositions,
                    acceptance,
                )
            try:
                plan = self.build_plan(repair_groups, diagnostic, working, round_number)
                binding_working_assets = [asset.model_copy(deep=True) for asset in working.assets]
                groups = build_initialization_groups(plan, working_assets=working.assets)
                initialized: list[
                    tuple[InitializationGroup, CandidateInitializationResult]
                ] = []
                prior_candidates: dict[str, CandidateAsset] = {}
                prior_operations: dict[str, str] = {}
                for group in groups:
                    optimizer_view = _optimizer_working_view(
                        working,
                        prior_candidates.values(),
                        prior_operations=prior_operations,
                    )
                    output = self.initialize(group, plan, optimizer_view, working)
                    output = _reuse_prior_candidate_assets(
                        group,
                        output,
                        prior_candidates,
                    )
                    initialized.append((group, output))
                    for candidate in output.candidates:
                        prior_candidates.setdefault(candidate.candidate_id, candidate)
                        prior_operations.setdefault(candidate.candidate_id, group.operation)
                bindings = freeze_candidate_bindings(
                    plan, initialized, working_assets=binding_working_assets
                )
            except ValueError as exc:
                return _result(
                    diagnostic,
                    working,
                    round_number,
                    trials_run,
                    False,
                    f"planning_or_initialization_invalid: {exc}",
                    dispositions,
                    acceptance,
                )

            candidates = {
                candidate.candidate_id: candidate
                for _, output in initialized
                for candidate in output.candidates
            }
            active_ids = set(candidates)
            accepted_ids: set[str] = set()
            round_dispositions: dict[str, CandidateDisposition] = {}
            application_companions_added = False

            for iteration in range(1, max_iterations + 1):
                snapshot = materialize_residual_trial(
                    working,
                    active_candidates=[candidates[item] for item in sorted(active_ids)],
                    bindings=bindings,
                )
                evaluation = self.execute_trial(snapshot, round_number, iteration)
                trials_run += 1
                if _generation_failed(evaluation):
                    _mark(
                        round_dispositions,
                        active_ids,
                        bindings,
                        "unresolved",
                        "candidate_generation_failed",
                    )
                    dispositions.extend(_number(round_dispositions, round_number))
                    return _result(
                        evaluation,
                        working,
                        round_number,
                        trials_run,
                        False,
                        "candidate_generation_failed",
                        dispositions,
                        acceptance,
                        remaining_targets,
                    )

                contract_failure = evaluation.get("candidate_failure") or {}
                if contract_failure.get("kind") == "ontology_contract":
                    if iteration == max_iterations or not self._refine_contract_failure(
                        contract_failure,
                        candidates,
                        active_ids,
                        bindings,
                        plan,
                        round_dispositions,
                    ):
                        diagnostic = evaluation
                        break
                    continue
                try:
                    assessed = self.assess_trial(
                        evaluation, plan, bindings, candidates, accepted_ids
                    )
                    policy_candidate_ids = tuple(
                        sorted(
                            item.candidate_id
                            for item in assessed.candidate_assessments
                            if item.problem == "application_policy"
                            and item.candidate_id in active_ids
                        )
                    )
                    if policy_candidate_ids:
                        if application_companions_added:
                            policy_candidate_ids = ()
                        elif iteration == max_iterations:
                            raise ValueError(
                                "application companion requires another trial iteration"
                            )
                        else:
                            previous_bindings = bindings.model_copy(deep=True)
                            companion_requirement_ids: set[str] = set()
                            for candidate_id in policy_candidate_ids:
                                binding = bindings.candidates_by_id[candidate_id]
                                if binding.owner not in {
                                    OptimizationOwner.SEMANTIC_MAPPING,
                                    OptimizationOwner.METRIC,
                                }:
                                    raise ValueError(
                                        "application_policy companion requires a Concept or "
                                        "Metric anchor"
                                    )
                                if len(binding.owned_target_ids) != 1:
                                    raise ValueError(
                                        "application_policy companion requires one owned Target"
                                    )
                                target_id = binding.owned_target_ids[0]
                                assessment = next(
                                    item
                                    for item in assessed.candidate_assessments
                                    if item.candidate_id == candidate_id
                                )
                                plan = add_application_policy_companion(
                                    plan,
                                    target_id=target_id,
                                    evidence=assessment.evidence,
                                )
                                companion_requirement_ids.add(
                                    f"{target_id}:application_rule"
                                )

                            companion_groups = [
                                group
                                for group in build_initialization_groups(
                                    plan,
                                    working_assets=working.assets,
                                )
                                if companion_requirement_ids.intersection(group.requirement_ids)
                            ]
                            prior_operations = {
                                candidate.candidate_id: group.operation
                                for group, output in initialized
                                for candidate in output.candidates
                            }
                            prior_candidates = dict(candidates)
                            for group in companion_groups:
                                optimizer_view = _optimizer_working_view(
                                    working,
                                    prior_candidates.values(),
                                    prior_operations=prior_operations,
                                )
                                output = self.initialize(group, plan, optimizer_view, working)
                                output = _reuse_prior_candidate_assets(
                                    group,
                                    output,
                                    prior_candidates,
                                )
                                initialized.append((group, output))
                                for candidate in output.candidates:
                                    candidates[candidate.candidate_id] = candidate
                                    prior_candidates[candidate.candidate_id] = candidate
                                    prior_operations[candidate.candidate_id] = group.operation
                                    active_ids.add(candidate.candidate_id)
                            bindings = freeze_candidate_bindings(
                                plan,
                                initialized,
                                working_assets=binding_working_assets,
                            )
                            _validate_binding_augmentation(
                                previous_bindings,
                                bindings,
                                candidates=candidates,
                                policy_candidate_ids=policy_candidate_ids,
                            )
                            application_companions_added = True
                            continue
                    decision = decide_joint_iteration(
                        plan=plan,
                        bindings=bindings,
                        target_assessments=assessed.target_assessments,
                        candidate_assessments=assessed.candidate_assessments,
                        accepted_candidate_ids=accepted_ids,
                    )
                except ValueError as exc:
                    _mark(
                        round_dispositions,
                        active_ids,
                        bindings,
                        "inconclusive",
                        f"assessment_invalid: {exc}",
                    )
                    diagnostic = evaluation
                    break

                remaining_targets = {
                    item.target_id
                    for item in assessed.target_assessments
                    if item.status != "resolved"
                }
                newly_accepted = set(decision.accept_candidate_ids)
                newly_accepted = _reference_closed_acceptance(
                    newly_accepted,
                    already_accepted=accepted_ids,
                    candidates=candidates,
                    working=working,
                )
                if newly_accepted:
                    working = accept_residual_candidates(
                        working,
                        candidates=list(candidates.values()),
                        accepted_candidate_ids=newly_accepted,
                        bindings=bindings,
                    )
                    accepted_ids.update(newly_accepted)
                    active_ids.difference_update(newly_accepted)
                    if self.on_working_updated is not None:
                        self.on_working_updated(working.model_copy(deep=True))
                    _record_acceptance(
                        newly_accepted,
                        bindings,
                        assessed,
                        round_number,
                        round_dispositions,
                        acceptance,
                    )

                if decision.close_reason:
                    _mark(
                        round_dispositions,
                        active_ids,
                        bindings,
                        "rejected",
                        decision.close_reason,
                    )
                    diagnostic = evaluation
                    break
                if not active_ids:
                    final_evaluation = evaluation
                    if evaluation.get("correct") and self.execute_final is not None:
                        accepted_snapshot = materialize_residual_trial(
                            working, active_candidates=[], bindings=bindings
                        )
                        final_evaluation = self.execute_final(accepted_snapshot, round_number)
                    if final_evaluation.get("correct"):
                        dispositions.extend(_number(round_dispositions, round_number))
                        return _result(
                            final_evaluation,
                            working,
                            round_number,
                            trials_run,
                            True,
                            None,
                            dispositions,
                            acceptance,
                        )
                    diagnostic = final_evaluation
                    break
                if iteration == max_iterations:
                    _mark(
                        round_dispositions,
                        active_ids,
                        bindings,
                        "unresolved",
                        "inner_iteration_budget_exhausted",
                    )
                    diagnostic = evaluation
                    break
                if not self._refine(
                    decision.refine_candidate_ids,
                    candidates,
                    bindings,
                    assessed,
                    plan,
                    round_dispositions,
                ):
                    diagnostic = evaluation
                    break
            dispositions.extend(_number(round_dispositions, round_number))

        return _result(
            diagnostic,
            working,
            max_rounds,
            trials_run,
            False,
            "heavy_round_budget_exhausted",
            dispositions,
            acceptance,
            remaining_targets,
        )

    def _refine_contract_failure(
        self,
        failure: dict[str, Any],
        candidates: dict[str, CandidateAsset],
        active_ids: set[str],
        bindings: Any,
        plan: HeavyRoundPlan,
        dispositions: dict[str, CandidateDisposition],
    ) -> bool:
        asset_id = str(failure.get("asset_id") or "").strip()
        culprit_ids = [
            item
            for item in active_ids
            if asset_id
            in {
                bindings.candidates_by_id[item].logical_asset_id,
                *bindings.candidates_by_id[item].dependency_asset_ids,
            }
        ]
        if len(culprit_ids) != 1:
            return False
        candidate_id = culprit_ids[0]
        try:
            candidates[candidate_id] = self.refine_candidate(
                candidates[candidate_id],
                CandidateAssessment(
                    candidate_id=candidate_id,
                    application="inconclusive",
                    quality_status="invalid",
                    quality_reason=str(failure.get("message") or "contract error"),
                    quality_defects=[
                        {
                            "asset_id": str(
                                failure.get("asset_id")
                                or bindings.candidates_by_id[candidate_id].logical_asset_id
                            ),
                            "field_or_clause": str(
                                failure.get("field_path") or "reported field"
                            ),
                            "reason": str(failure.get("message") or "invalid value"),
                        }
                    ],
                    problem="content",
                    evidence=str(failure.get("message") or "contract error"),
                    modification_instruction=(
                        "Fix the canonical ontology contract error at "
                        f"{failure.get('field_path') or 'reported field'}: "
                        f"{failure.get('message') or 'invalid value'}."
                    ),
                ),
                [],
                plan,
            )
            return True
        except ValueError as exc:
            binding = bindings.candidates_by_id[candidate_id]
            dispositions[candidate_id] = CandidateDisposition(
                candidate_id=candidate_id,
                logical_asset_id=binding.logical_asset_id,
                disposition="unresolved",
                reason=f"contract_refinement_exhausted: {exc}",
            )
            return False

    def _refine(
        self,
        candidate_ids: tuple[str, ...],
        candidates: dict[str, CandidateAsset],
        bindings: Any,
        assessment: ResidualJointAssessment,
        plan: HeavyRoundPlan,
        dispositions: dict[str, CandidateDisposition],
    ) -> bool:
        by_candidate = {item.candidate_id: item for item in assessment.candidate_assessments}
        for candidate_id in candidate_ids:
            target_rows = [
                item
                for item in assessment.target_assessments
                if item.target_id in bindings.candidates_by_id[candidate_id].owned_target_ids
            ]
            try:
                refined = self.refine_candidate(
                    candidates[candidate_id],
                    by_candidate[candidate_id],
                    target_rows,
                    plan,
                )
                binding = bindings.candidates_by_id[candidate_id]
                if refined.candidate_id != candidate_id:
                    raise ValueError("refinement changed frozen candidate ID")
                if refined.asset.id != binding.logical_asset_id:
                    raise ValueError("refinement changed frozen logical asset ID")
                candidates[candidate_id] = refined
            except ValueError as exc:
                binding = bindings.candidates_by_id[candidate_id]
                dispositions[candidate_id] = CandidateDisposition(
                    candidate_id=candidate_id,
                    logical_asset_id=binding.logical_asset_id,
                    disposition="unresolved",
                    reason=f"refinement_exhausted: {exc}",
                )
                return False
        return True


def _optimizer_working_view(
    working: CaseWorkingOntology,
    prior_candidates: Iterable[CandidateAsset],
    *,
    prior_operations: dict[str, str] | None = None,
) -> CaseWorkingOntology:
    """Expose earlier same-round proposals without mutating accepted Working."""

    visible = {asset.id: asset.model_copy(deep=True) for asset in working.assets}
    operations = prior_operations or {}
    for candidate in prior_candidates:
        primary = candidate.asset
        existing = visible.get(primary.id)
        if existing is not None and asset_fingerprint(existing) != asset_fingerprint(primary):
            operation = operations.get(candidate.candidate_id)
            if existing.node_type != primary.node_type:
                raise ValueError(
                    "same-round visible asset conflicts with a different Working asset type: "
                    f"{primary.id}"
                )
            if operation in {"create", "update"}:
                visible[primary.id] = primary.model_copy(deep=True)
            else:
                raise ValueError(
                    "same-round visible asset conflicts with an existing logical ID: "
                    f"{primary.id}"
                )
        else:
            visible.setdefault(primary.id, primary.model_copy(deep=True))
        for dependency in candidate.dependency_assets:
            existing = visible.get(dependency.id)
            if existing is not None and asset_fingerprint(existing) != asset_fingerprint(
                dependency
            ):
                raise ValueError(
                    "same-round visible dependency conflicts with an existing logical ID: "
                    f"{dependency.id}"
                )
            visible.setdefault(dependency.id, dependency.model_copy(deep=True))
    return CaseWorkingOntology(assets=list(visible.values()))


def _validate_binding_augmentation(
    previous: Any,
    current: Any,
    *,
    candidates: dict[str, CandidateAsset],
    policy_candidate_ids: tuple[str, ...],
) -> None:
    """Keep the original bindings frozen while adding one Rule per policy Target."""

    for candidate_id, binding in previous.candidates_by_id.items():
        if current.candidates_by_id.get(candidate_id) != binding:
            raise ValueError(
                f"application companion changed an existing binding: {candidate_id}"
            )
    new_candidate_ids = set(current.candidates_by_id) - set(previous.candidates_by_id)
    expected_targets = {
        previous.candidates_by_id[candidate_id].owned_target_ids[0]
        for candidate_id in policy_candidate_ids
    }
    if len(new_candidate_ids) != len(expected_targets):
        raise ValueError("application companion must add exactly one Rule per Target")
    for target_id in expected_targets:
        old_neighbors = set(previous.candidate_ids_by_target[target_id])
        new_neighbors = set(current.candidate_ids_by_target[target_id]) - old_neighbors
        if len(new_neighbors) != 1:
            raise ValueError("application companion Target must gain exactly one Candidate")
        companion = current.candidates_by_id[next(iter(new_neighbors))]
        if companion.owner != OptimizationOwner.GENERATION_RULE:
            raise ValueError("application companion Candidate must be a Generation Rule")
        if companion.operation != "create":
            raise ValueError("application companion Candidate must be CREATE")
        anchor_ids = {
            previous.candidates_by_id[candidate_id].logical_asset_id
            for candidate_id in policy_candidate_ids
            if target_id
            in previous.candidates_by_id[candidate_id].owned_target_ids
        }
        if len(anchor_ids) != 1:
            raise ValueError("application companion Target must have one anchor asset")
        companion_asset = candidates[companion.candidate_id].asset
        references = set(getattr(companion_asset.properties, "references", []))
        if not anchor_ids <= references:
            raise ValueError("application companion Rule must exact-reference its anchor asset")


def _reuse_prior_candidate_assets(
    group: InitializationGroup,
    result: CandidateInitializationResult,
    prior_candidates: dict[str, CandidateAsset],
) -> CandidateInitializationResult:
    """Reuse exact earlier proposals instead of freezing duplicate logical assets."""

    prior_by_asset_id: dict[str, tuple[CandidateAsset, NodeAsset]] = {}
    for prior in prior_candidates.values():
        for asset in [prior.asset, *prior.dependency_assets]:
            prior_by_asset_id[asset.id] = (prior, asset)

    normalized_by_id: dict[str, CandidateAsset] = {}
    replacement_ids: dict[str, str] = {}
    for candidate in result.candidates:
        dependencies = []
        for dependency in candidate.dependency_assets:
            prior_row = prior_by_asset_id.get(dependency.id)
            if prior_row is None:
                dependencies.append(dependency)
                continue
            _, prior_asset = prior_row
            if asset_fingerprint(prior_asset) != asset_fingerprint(dependency):
                raise ValueError(
                    "same-round dependency redefines a visible logical asset: "
                    f"{dependency.id}"
                )

        primary_row = prior_by_asset_id.get(candidate.asset.id)
        if primary_row is None:
            normalized = candidate.model_copy(
                update={"dependency_assets": dependencies},
                deep=True,
            )
            normalized_by_id[normalized.candidate_id] = normalized
            continue

        prior_candidate, prior_asset = primary_row
        if asset_fingerprint(prior_asset) != asset_fingerprint(candidate.asset):
            raise ValueError(
                "same-round Candidate redefines a visible logical asset: "
                f"{candidate.asset.id}"
            )
        if dependencies:
            raise ValueError(
                "a requirement already satisfied by a same-round Candidate cannot add "
                "new dependency assets"
            )
        replacement_ids[candidate.candidate_id] = prior_candidate.candidate_id
        normalized_by_id[prior_candidate.candidate_id] = prior_candidate.model_copy(deep=True)

    requirement_bindings = {
        requirement_id: replacement_ids.get(candidate_id, candidate_id)
        for requirement_id, candidate_id in result.candidate_id_by_requirement.items()
    }
    normalized = CandidateInitializationResult(
        candidates=list(normalized_by_id.values()),
        candidate_id_by_requirement=requirement_bindings,
    )
    # The frozen group still owns its requirement even when it reuses a prior Candidate.
    validate_initialization_result(group, normalized)
    return normalized


def _reference_closed_acceptance(
    proposed: set[str],
    *,
    already_accepted: set[str],
    candidates: dict[str, CandidateAsset],
    working: CaseWorkingOntology,
) -> set[str]:
    """Keep only Candidates whose same-round logical references enter Working together."""

    working_ids = {asset.id for asset in working.assets}
    provider_by_asset_id = {
        asset.id: candidate.candidate_id
        for candidate in candidates.values()
        for asset in [candidate.asset, *candidate.dependency_assets]
    }
    closed = set(proposed)
    while True:
        available_candidate_ids = already_accepted | closed
        rejected = {
            candidate_id
            for candidate_id in closed
            if any(
                reference not in working_ids
                and provider_by_asset_id.get(reference) not in available_candidate_ids
                for asset in [
                    candidates[candidate_id].asset,
                    *candidates[candidate_id].dependency_assets,
                ]
                for reference in getattr(asset.properties, "references", [])
                if reference.startswith(("concept.", "metric.", "generation_rule."))
            )
        }
        if not rejected:
            return closed
        closed.difference_update(rejected)


def _record_acceptance(
    candidate_ids: set[str],
    bindings: Any,
    assessment: ResidualJointAssessment,
    round_number: int,
    dispositions: dict[str, CandidateDisposition],
    evidence: list[AcceptanceEvidence],
) -> None:
    candidates = {item.candidate_id: item for item in assessment.candidate_assessments}
    targets = {item.target_id: item for item in assessment.target_assessments}
    for candidate_id in sorted(candidate_ids):
        binding = bindings.candidates_by_id[candidate_id]
        candidate = candidates[candidate_id]
        resolved = tuple(
            sorted(
                target_id
                for target_id in binding.owned_target_ids
                if targets[target_id].status == "resolved"
            )
        )
        evidence.append(
            AcceptanceEvidence(
                round_number=round_number,
                candidate_id=candidate_id,
                logical_asset_id=binding.logical_asset_id,
                composition_asset_ids=(
                    binding.logical_asset_id,
                    *binding.dependency_asset_ids,
                ),
                owned_target_ids=tuple(sorted(binding.owned_target_ids)),
                resolved_target_ids=resolved,
                application=candidate.application,
                evidence=candidate.evidence,
                regressed_target_ids=tuple(sorted(candidate.regressed_target_ids)),
            )
        )
        dispositions[candidate_id] = CandidateDisposition(
            candidate_id=candidate_id,
            logical_asset_id=binding.logical_asset_id,
            disposition="accepted",
            reason=candidate.evidence,
        )


def _generation_failed(evaluation: dict[str, Any]) -> bool:
    failure_kind = (evaluation.get("candidate_failure") or {}).get("kind")
    if failure_kind == "ontology_contract":
        return False
    if failure_kind == "generation":
        return True
    query = str(evaluation.get("query") or "").strip()
    return not query and (
        failure_kind == "validation"
        or evaluation.get("evaluation_available") is False
        or evaluation.get("evaluation_status") == "candidate_validation_failed"
    )


def _mark(dispositions, candidate_ids, bindings, disposition, reason) -> None:
    for candidate_id in candidate_ids:
        binding = bindings.candidates_by_id[candidate_id]
        dispositions[candidate_id] = CandidateDisposition(
            candidate_id=candidate_id,
            logical_asset_id=binding.logical_asset_id,
            disposition=disposition,
            reason=reason,
        )


def _number(dispositions, round_number: int) -> tuple[CandidateDisposition, ...]:
    return tuple(
        dispositions[item].model_copy(update={"round_number": round_number})
        for item in sorted(dispositions)
    )


def _result(
    evaluation,
    working,
    rounds_run,
    trials_run,
    completed,
    failure_reason,
    dispositions,
    acceptance,
    remaining_targets=None,
) -> RepairCycleResult:
    return RepairCycleResult(
        evaluation=evaluation,
        working=working,
        rounds_run=rounds_run,
        trials_run=trials_run,
        completed=completed,
        failure_reason=failure_reason,
        candidate_dispositions=tuple(dispositions),
        acceptance_evidence=tuple(acceptance),
        remaining_target_ids=tuple(sorted(remaining_targets or set())),
    )
