from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import Field, model_validator

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.repair.contracts import (
    OptimizationOwner,
    asset_fingerprint,
)

OwnerOperation = Literal["create", "update"]


class RepairTarget(NodeModel):
    target_id: str = Field(min_length=1)
    error_key: str = Field(min_length=1)
    repair_instruction: str = Field(min_length=1)
    evidence: str = Field(min_length=1)


class AssetRequirement(NodeModel):
    requirement_id: str = Field(min_length=1)
    operation: OwnerOperation
    asset_id: str | None = None
    instruction: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_operation(self) -> AssetRequirement:
        if self.operation == "create" and self.asset_id is not None:
            raise ValueError("CREATE requirement cannot reference an existing asset")
        if self.operation == "update" and not (self.asset_id or "").strip():
            raise ValueError("UPDATE requirement requires an existing asset ID")
        return self


class OwnerPlan(NodeModel):
    target_id: str = Field(min_length=1)
    owner: OptimizationOwner
    required_assets: list[AssetRequirement] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_owner_plan(self) -> OwnerPlan:
        if self.owner not in _OWNER_NODE_TYPE:
            raise ValueError("OwnerPlan requires an optimizable owner")
        requirement_ids = [item.requirement_id for item in self.required_assets]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("OwnerPlan contains duplicate requirement IDs")
        operations = {item.operation for item in self.required_assets}
        if len(operations) != 1:
            raise ValueError("one OwnerPlan cannot mix CREATE and UPDATE requirements")
        return self


class HeavyRoundPlan(NodeModel):
    round_id: str = Field(min_length=1)
    diagnostic_sql: str
    targets: list[RepairTarget] = Field(min_length=1)
    owner_plans: list[OwnerPlan] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_plan_shape(self) -> HeavyRoundPlan:
        target_ids = [item.target_id for item in self.targets]
        plan_target_ids = [item.target_id for item in self.owner_plans]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("HeavyRoundPlan contains duplicate target IDs")
        if set(target_ids) != set(plan_target_ids):
            raise ValueError(
                "every target must have exactly one OwnerPlan unless it has the allowed "
                "asset application companion pair"
            )
        plans_by_target: dict[str, list[OwnerPlan]] = defaultdict(list)
        for owner_plan in self.owner_plans:
            plans_by_target[owner_plan.target_id].append(owner_plan)
        for target_id, plans in plans_by_target.items():
            if len(plans) == 1:
                continue
            owners = [plan.owner for plan in plans]
            if owners not in (
                [OptimizationOwner.SEMANTIC_MAPPING, OptimizationOwner.GENERATION_RULE],
                [OptimizationOwner.METRIC, OptimizationOwner.GENERATION_RULE],
            ):
                raise ValueError(
                    "multiple OwnerPlans are allowed only for an ordered asset application "
                    f"companion pair: {target_id}"
                )
            companion_operations = {
                requirement.operation for requirement in plans[1].required_assets
            }
            if companion_operations != {"create"}:
                raise ValueError("asset application companion must be CREATE")
        requirement_ids = [
            requirement.requirement_id
            for plan in self.owner_plans
            for requirement in plan.required_assets
        ]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("HeavyRoundPlan contains duplicate requirement IDs")
        return self


class CandidateAsset(NodeModel):
    candidate_id: str = Field(min_length=1)
    asset: NodeAsset
    dependency_assets: list[NodeAsset] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_composition(self) -> CandidateAsset:
        if self.dependency_assets and self.asset.node_type != NodeType.METRIC:
            raise ValueError("only a Metric candidate may contain dependency assets")
        if any(asset.node_type != NodeType.METRIC for asset in self.dependency_assets):
            raise ValueError("Metric dependency_assets may contain only Metrics")
        ids = [self.asset.id, *(asset.id for asset in self.dependency_assets)]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate composition contains duplicate logical asset IDs")
        return self


class CandidateInitializationResult(NodeModel):
    candidates: list[CandidateAsset] = Field(min_length=1)
    candidate_id_by_requirement: dict[str, str]

    @model_validator(mode="after")
    def validate_candidate_ids(self) -> CandidateInitializationResult:
        candidate_ids = [item.candidate_id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("initialization result contains duplicate candidate IDs")
        return self


class InitializationGroup(NodeModel):
    owner: OptimizationOwner
    operation: OwnerOperation
    target_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    base_asset_id: str | None = None
    base_asset_fingerprint: str | None = None

    @model_validator(mode="after")
    def validate_group(self) -> InitializationGroup:
        if not self.target_ids or len(self.target_ids) != len(set(self.target_ids)):
            raise ValueError("InitializationGroup requires unique target IDs")
        if not self.requirement_ids or len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("InitializationGroup requires unique requirement IDs")
        if self.operation == "create" and (
            self.base_asset_id is not None or self.base_asset_fingerprint is not None
        ):
            raise ValueError("CREATE group cannot carry a base asset")
        if self.operation == "update" and not (self.base_asset_id and self.base_asset_fingerprint):
            raise ValueError("UPDATE group requires a frozen base asset")
        return self


class CandidateBinding(NodeModel):
    candidate_id: str
    owner: OptimizationOwner
    operation: OwnerOperation
    logical_asset_id: str
    dependency_asset_ids: tuple[str, ...] = ()
    base_asset_fingerprint: str | None = None
    requirement_ids: tuple[str, ...]
    owned_target_ids: tuple[str, ...]


class FrozenCandidateBindings(NodeModel):
    candidates_by_id: dict[str, CandidateBinding]
    candidate_ids_by_target: dict[str, tuple[str, ...]]
    candidate_id_by_requirement: dict[str, str]


_OWNER_NODE_TYPE = {
    OptimizationOwner.SEMANTIC_MAPPING: NodeType.CONCEPT,
    OptimizationOwner.METRIC: NodeType.METRIC,
    OptimizationOwner.GENERATION_RULE: NodeType.GENERATION_RULE,
}


def validate_heavy_round_plan(
    plan: HeavyRoundPlan,
    *,
    working_assets: list[NodeAsset],
) -> None:
    available_by_id = _assets_by_id(working_assets)
    for owner_plan in plan.owner_plans:
        expected_type = _OWNER_NODE_TYPE[owner_plan.owner]
        for requirement in owner_plan.required_assets:
            if requirement.operation == "create":
                continue
            asset = available_by_id.get(requirement.asset_id or "")
            if asset is None:
                raise ValueError(f"UPDATE asset does not exist: {requirement.asset_id}")
            if asset.node_type != expected_type:
                raise ValueError(f"UPDATE asset type does not match owner: {requirement.asset_id}")


def build_initialization_groups(
    plan: HeavyRoundPlan,
    *,
    working_assets: list[NodeAsset],
) -> list[InitializationGroup]:
    validate_heavy_round_plan(
        plan,
        working_assets=working_assets,
    )
    available_by_id = _assets_by_id(working_assets)
    update_groups: dict[tuple[OptimizationOwner, str], dict[str, list[str]]] = {}
    groups: list[InitializationGroup] = []

    for owner_plan in plan.owner_plans:
        requirements = owner_plan.required_assets
        operation = requirements[0].operation
        if operation == "create":
            groups.append(
                InitializationGroup(
                    owner=owner_plan.owner,
                    operation="create",
                    target_ids=(owner_plan.target_id,),
                    requirement_ids=tuple(item.requirement_id for item in requirements),
                )
            )
            continue
        for requirement in requirements:
            asset_id = requirement.asset_id or ""
            bucket = update_groups.setdefault(
                (owner_plan.owner, asset_id), {"targets": [], "requirements": []}
            )
            bucket["targets"].append(owner_plan.target_id)
            bucket["requirements"].append(requirement.requirement_id)

    for (owner, asset_id), bucket in update_groups.items():
        base = available_by_id[asset_id]
        groups.append(
            InitializationGroup(
                owner=owner,
                operation="update",
                target_ids=tuple(dict.fromkeys(bucket["targets"])),
                requirement_ids=tuple(bucket["requirements"]),
                base_asset_id=asset_id,
                base_asset_fingerprint=asset_fingerprint(base),
            )
        )
    return groups


def validate_initialization_result(
    group: InitializationGroup,
    result: CandidateInitializationResult,
) -> None:
    expected_requirements = set(group.requirement_ids)
    actual_requirements = set(result.candidate_id_by_requirement)
    if actual_requirements != expected_requirements:
        raise ValueError(
            "initialization bindings must exactly cover group requirements; "
            f"missing={sorted(expected_requirements - actual_requirements)}, "
            f"extra={sorted(actual_requirements - expected_requirements)}"
        )
    candidates_by_id = {item.candidate_id: item for item in result.candidates}
    unknown = sorted(set(result.candidate_id_by_requirement.values()) - candidates_by_id.keys())
    if unknown:
        raise ValueError(f"requirement bindings reference unknown candidates: {unknown}")
    if set(candidates_by_id) != set(result.candidate_id_by_requirement.values()):
        raise ValueError("every returned candidate must bind at least one requirement")
    if group.operation == "update" and len(candidates_by_id) != 1:
        raise ValueError("UPDATE group must produce exactly one candidate")
    expected_type = _OWNER_NODE_TYPE[group.owner]
    for candidate in result.candidates:
        if candidate.asset.node_type != expected_type:
            raise ValueError("candidate asset type does not match initialization owner")
        if group.operation == "update" and candidate.asset.id != group.base_asset_id:
            raise ValueError("UPDATE candidate changed its logical asset ID")
        if candidate.dependency_assets and group.owner != OptimizationOwner.METRIC:
            raise ValueError("only the Metric optimizer may create dependency assets")


def freeze_candidate_bindings(
    plan: HeavyRoundPlan,
    group_results: list[tuple[InitializationGroup, CandidateInitializationResult]],
    *,
    working_assets: list[NodeAsset] | None = None,
) -> FrozenCandidateBindings:
    requirement_target = {
        requirement.requirement_id: owner_plan.target_id
        for owner_plan in plan.owner_plans
        for requirement in owner_plan.required_assets
    }
    candidates_by_id: dict[str, CandidateBinding] = {}
    candidate_id_by_requirement: dict[str, str] = {}
    logical_asset_ids: set[str] = set()
    working_by_id = _assets_by_id(working_assets or [])

    for group, result in group_results:
        validate_initialization_result(group, result)
        result_candidates = {item.candidate_id: item for item in result.candidates}
        requirements_by_candidate: dict[str, list[str]] = defaultdict(list)
        for requirement_id, candidate_id in result.candidate_id_by_requirement.items():
            if requirement_id in candidate_id_by_requirement:
                raise ValueError(f"requirement bound by multiple groups: {requirement_id}")
            candidate_id_by_requirement[requirement_id] = candidate_id
            requirements_by_candidate[candidate_id].append(requirement_id)
        for candidate_id, requirement_ids in requirements_by_candidate.items():
            candidate = result_candidates[candidate_id]
            composition_ids = [
                candidate.asset.id,
                *(asset.id for asset in candidate.dependency_assets),
            ]
            if candidate_id in candidates_by_id:
                binding = candidates_by_id[candidate_id]
                if (
                    binding.owner != group.owner
                    or binding.operation != group.operation
                    or binding.logical_asset_id != candidate.asset.id
                    or binding.dependency_asset_ids != tuple(composition_ids[1:])
                    or binding.base_asset_fingerprint != group.base_asset_fingerprint
                ):
                    raise ValueError(
                        f"same-round reused candidate binding changed: {candidate_id}"
                    )
                binding.requirement_ids = tuple(
                    dict.fromkeys([*binding.requirement_ids, *requirement_ids])
                )
                binding.owned_target_ids = tuple(
                    dict.fromkeys(
                        [
                            *binding.owned_target_ids,
                            *(requirement_target[item] for item in requirement_ids),
                        ]
                    )
                )
                continue
            duplicates = logical_asset_ids.intersection(composition_ids)
            if duplicates:
                raise ValueError(
                    f"duplicate logical asset ID across candidates: {sorted(duplicates)}"
                )
            logical_asset_ids.update(composition_ids)
            operation = group.operation
            base_asset_fingerprint = group.base_asset_fingerprint
            if operation == "create" and candidate.asset.id in working_by_id:
                working_asset = working_by_id[candidate.asset.id]
                if working_asset.node_type != candidate.asset.node_type:
                    raise ValueError(
                        "CREATE identity collision has a different Working asset type: "
                        f"{candidate.asset.id}"
                    )
                operation = "update"
                base_asset_fingerprint = asset_fingerprint(working_asset)
            dependency_collisions = sorted(set(composition_ids[1:]).intersection(working_by_id))
            if dependency_collisions:
                raise ValueError(
                    "candidate dependency_assets must be newly created; Working collisions: "
                    f"{dependency_collisions}"
                )
            candidates_by_id[candidate_id] = CandidateBinding(
                candidate_id=candidate_id,
                owner=group.owner,
                operation=operation,
                logical_asset_id=candidate.asset.id,
                dependency_asset_ids=tuple(composition_ids[1:]),
                base_asset_fingerprint=base_asset_fingerprint,
                requirement_ids=tuple(requirement_ids),
                owned_target_ids=tuple(
                    dict.fromkeys(requirement_target[item] for item in requirement_ids)
                ),
            )

    expected_requirements = set(requirement_target)
    if set(candidate_id_by_requirement) != expected_requirements:
        raise ValueError("group results do not cover every planned requirement")
    candidate_ids_by_target: dict[str, list[str]] = defaultdict(list)
    for binding in candidates_by_id.values():
        for target_id in binding.owned_target_ids:
            candidate_ids_by_target[target_id].append(binding.candidate_id)
    expected_targets = {target.target_id for target in plan.targets}
    if set(candidate_ids_by_target) != expected_targets:
        raise ValueError("frozen bindings do not cover every repair target")
    return FrozenCandidateBindings(
        candidates_by_id=candidates_by_id,
        candidate_ids_by_target={
            target_id: tuple(candidate_ids)
            for target_id, candidate_ids in candidate_ids_by_target.items()
        },
        candidate_id_by_requirement=candidate_id_by_requirement,
    )


def _assets_by_id(assets: list[NodeAsset]) -> dict[str, NodeAsset]:
    result = {asset.id: asset for asset in assets}
    if len(result) != len(assets):
        raise ValueError("working ontology contains duplicate asset IDs")
    return result


class OptimizationRoutingError(ValueError):
    """A Repair Group does not satisfy the deterministic plan contract."""


def add_application_policy_companion(
    plan: HeavyRoundPlan,
    *,
    target_id: str,
    evidence: str,
) -> HeavyRoundPlan:
    """Add the one allowed round-local Generation Rule companion plan."""

    matching = [item for item in plan.owner_plans if item.target_id == target_id]
    if len(matching) != 1 or matching[0].owner not in {
        OptimizationOwner.SEMANTIC_MAPPING,
        OptimizationOwner.METRIC,
    }:
        raise ValueError("application companion requires one Concept or Metric OwnerPlan")
    target = next((item for item in plan.targets if item.target_id == target_id), None)
    if target is None:
        raise ValueError(f"application companion target does not exist: {target_id}")
    anchor_label = (
        "Concept"
        if matching[0].owner == OptimizationOwner.SEMANTIC_MAPPING
        else "Metric"
    )
    requirement_id = f"{target_id}:application_rule"
    instruction = (
        f"Create one application policy for the visible {anchor_label} and the evidenced "
        f"non-equivalent alternative implementation. Require the {anchor_label} contract when "
        "that alternative competes at the same decision point. Own only this selection behavior, "
        f"exact-reference the {anchor_label}, and do not repeat its intrinsic content. "
        "Application evidence: "
        f"{evidence.strip()}"
    )
    return HeavyRoundPlan.model_validate(
        {
            **plan.model_dump(mode="json"),
            "owner_plans": [
                *(item.model_dump(mode="json") for item in plan.owner_plans),
                OwnerPlan(
                    target_id=target_id,
                    owner=OptimizationOwner.GENERATION_RULE,
                    required_assets=[
                        AssetRequirement(
                            requirement_id=requirement_id,
                            operation="create",
                            instruction=instruction,
                        )
                    ],
                ).model_dump(mode="json"),
            ],
        }
    )


def add_metric_application_companion(
    plan: HeavyRoundPlan,
    *,
    target_id: str,
    evidence: str,
) -> HeavyRoundPlan:
    """Backward-compatible name for the generic application-policy companion."""

    return add_application_policy_companion(
        plan,
        target_id=target_id,
        evidence=evidence,
    )


def build_optimization_plan(
    *,
    repair_groups: list[dict[str, Any]],
    working_assets: list[NodeAsset],
    round_id: str,
    diagnostic_sql: str,
) -> HeavyRoundPlan:
    """Compile reviewed Repair Groups without changing their selected owners."""

    targets: list[RepairTarget] = []
    owner_plans: list[OwnerPlan] = []
    seen_ids: set[str] = set()
    for group in repair_groups:
        target_id = str(group.get("id") or "").strip()
        instruction = str(group.get("repair_instruction") or "").strip()
        owner_value = str(group.get("owner") or "").strip()
        if not target_id or target_id in seen_ids:
            raise OptimizationRoutingError("Repair Groups require unique non-empty IDs")
        seen_ids.add(target_id)
        if not instruction:
            raise OptimizationRoutingError(
                f"Repair Group {target_id} requires a repair instruction"
            )
        try:
            owner = OptimizationOwner(owner_value)
        except ValueError as exc:
            raise OptimizationRoutingError(
                f"Repair Group {target_id} has invalid owner: {owner_value or '<missing>'}"
            ) from exc
        if owner not in _OWNER_NODE_TYPE:
            raise OptimizationRoutingError(
                f"Repair Group {target_id} requires one optimizable owner"
            )
        targets.append(
            RepairTarget(
                target_id=target_id,
                error_key=str(group.get("error_key") or "unknown_error"),
                repair_instruction=instruction,
                evidence=_repair_group_evidence(group),
            )
        )
        base_asset_id = str(group.get("base_asset_id") or "").strip() or None
        owner_plans.append(
            OwnerPlan(
                target_id=target_id,
                owner=owner,
                required_assets=[
                    AssetRequirement(
                        requirement_id=f"{target_id}:asset:1",
                        operation="update" if base_asset_id else "create",
                        asset_id=base_asset_id,
                        instruction=instruction,
                    )
                ],
            )
        )

    if not targets:
        raise OptimizationRoutingError("at least one Repair Group is required")
    plan = HeavyRoundPlan(
        round_id=round_id,
        diagnostic_sql=diagnostic_sql,
        targets=targets,
        owner_plans=owner_plans,
    )
    validate_heavy_round_plan(plan, working_assets=working_assets)
    return plan


def _repair_group_evidence(group: dict[str, Any]) -> str:
    evidence = group.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        return evidence.strip()
    parts = [
        str(group.get(key) or "").strip()
        for key in ("grouping_reason", "question_evidence", "repair_instruction")
    ]
    return " | ".join(part for part in parts if part) or "Repair Group evidence"
