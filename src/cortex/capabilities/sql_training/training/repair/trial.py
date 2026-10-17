from __future__ import annotations

from pydantic import Field

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.training.repair.contracts import (
    asset_fingerprint,
    ontology_fingerprint,
)
from cortex.capabilities.sql_training.training.repair.plan import (
    CandidateAsset,
    FrozenCandidateBindings,
)


class CaseWorkingOntology(NodeModel):
    """The accepted ontology inside one isolated training case."""

    assets: list[NodeAsset] = Field(default_factory=list)


class ResidualTrialSnapshot(NodeModel):
    assets: list[NodeAsset]
    ontology_fingerprint: str
    active_candidate_ids: tuple[str, ...]


def materialize_residual_trial(
    working: CaseWorkingOntology,
    *,
    active_candidates: list[CandidateAsset],
    bindings: FrozenCandidateBindings,
) -> ResidualTrialSnapshot:
    """Overlay active candidates without mutating the case's accepted ontology."""

    materialized = _unique_assets(working.assets, label="working ontology")
    candidates_by_id = {candidate.candidate_id: candidate for candidate in active_candidates}
    if len(candidates_by_id) != len(active_candidates):
        raise ValueError("active candidates contain duplicate candidate IDs")
    if set(candidates_by_id) - set(bindings.candidates_by_id):
        raise ValueError("active candidates contain IDs outside frozen bindings")
    for candidate_id, candidate in candidates_by_id.items():
        binding = bindings.candidates_by_id[candidate_id]
        if candidate.asset.id != binding.logical_asset_id:
            raise ValueError("candidate logical identity differs from frozen binding")
        dependency_ids = tuple(asset.id for asset in candidate.dependency_assets)
        if dependency_ids != binding.dependency_asset_ids:
            raise ValueError("candidate dependency identities differ from frozen binding")
        if binding.operation == "create":
            if candidate.asset.id in materialized:
                raise ValueError(
                    f"CREATE candidate collides with working asset: {candidate.asset.id}"
                )
        else:
            base = materialized.get(candidate.asset.id)
            if base is None:
                raise ValueError(
                    f"UPDATE base is missing from working ontology: {candidate.asset.id}"
                )
            if asset_fingerprint(base) != binding.base_asset_fingerprint:
                raise ValueError(f"UPDATE base fingerprint is stale: {candidate.asset.id}")
        for dependency in candidate.dependency_assets:
            if dependency.id in materialized:
                raise ValueError(
                    f"CREATE dependency collides with working asset: {dependency.id}"
                )
        for asset in [candidate.asset, *candidate.dependency_assets]:
            materialized[asset.id] = asset.model_copy(deep=True)
    assets = sorted(materialized.values(), key=lambda asset: asset.id)
    return ResidualTrialSnapshot(
        assets=assets,
        ontology_fingerprint=ontology_fingerprint(assets),
        active_candidate_ids=tuple(sorted(candidates_by_id)),
    )


def accept_residual_candidates(
    working: CaseWorkingOntology,
    *,
    candidates: list[CandidateAsset],
    accepted_candidate_ids: set[str],
    bindings: FrozenCandidateBindings,
) -> CaseWorkingOntology:
    """Move a target-locally accepted subset into the case working ontology."""

    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if not accepted_candidate_ids <= set(candidates_by_id):
        raise ValueError("accepted candidate IDs must exist in the current candidate set")
    accepted_candidates = [candidates_by_id[item] for item in sorted(accepted_candidate_ids)]
    snapshot = materialize_residual_trial(
        working,
        active_candidates=accepted_candidates,
        bindings=bindings,
    )
    return CaseWorkingOntology(
        assets=snapshot.assets,
    )


def _unique_assets(assets: list[NodeAsset], *, label: str) -> dict[str, NodeAsset]:
    result = {asset.id: asset for asset in assets}
    if len(result) != len(assets):
        raise ValueError(f"{label} contains duplicate logical asset IDs")
    return result
