from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from cortex.capabilities.sql_training.contracts.metric import metric_dependency_refs
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel


class InitialCandidateComposition(NodeModel):
    """Extractor-declared composition and dependency roots for an initial candidate."""

    candidate_id: str = Field(min_length=1)
    assets: list[NodeAsset] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_composition(self) -> InitialCandidateComposition:
        asset_ids = [asset.id for asset in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("initial candidate composition contains duplicate asset IDs")
        if len(self.assets) > 1 and any(
            asset.node_type != NodeType.METRIC for asset in self.assets
        ):
            raise ValueError("only Metric candidates may form a multi-asset composition")
        if not self.root_asset_ids:
            raise ValueError("initial Metric composition must have at least one dependency root")
        return self

    @property
    def asset_ids(self) -> tuple[str, ...]:
        return tuple(asset.id for asset in self.assets)

    @property
    def root_asset_ids(self) -> tuple[str, ...]:
        if len(self.assets) == 1 and self.assets[0].node_type != NodeType.METRIC:
            return self.asset_ids
        member_ids = set(self.asset_ids)
        referenced_members = {
            dependency_id
            for asset in self.assets
            for dependency_id in metric_dependency_refs(asset)
            if dependency_id in member_ids
        }
        return tuple(asset_id for asset_id in self.asset_ids if asset_id not in referenced_members)

    @property
    def dependency_asset_ids(self) -> tuple[str, ...]:
        roots = set(self.root_asset_ids)
        return tuple(asset_id for asset_id in self.asset_ids if asset_id not in roots)


def build_initial_candidate_compositions(
    candidates: list[NodeAsset],
    *,
    metric_candidate_sets: list[dict[str, Any]] | None = None,
) -> list[InitialCandidateComposition]:
    """Build round-local atomic units using extractor-declared Metric set membership."""

    candidate_by_id = _validate_candidates(candidates)
    metrics = {
        asset_id: asset
        for asset_id, asset in candidate_by_id.items()
        if asset.node_type == NodeType.METRIC
    }
    compositions: list[InitialCandidateComposition] = []
    if metric_candidate_sets is None:
        if len(metrics) > 1:
            raise ValueError(
                "multiple Initial Metrics require explicit candidate set membership"
            )
        metric_candidate_sets = [
            {
                "candidate_id": asset_id,
                "asset_ids": [asset_id],
            }
            for asset_id in metrics
        ]
    declared_metric_ids: set[str] = set()
    for raw_set in metric_candidate_sets:
        candidate_id = str(raw_set.get("candidate_id") or "").strip()
        asset_ids = tuple(str(item) for item in raw_set.get("asset_ids") or [])
        if not candidate_id or not asset_ids:
            raise ValueError("Initial Metric candidate set metadata is incomplete")
        if set(asset_ids) - set(metrics):
            raise ValueError("Initial Metric candidate set references unknown assets")
        duplicates = declared_metric_ids.intersection(asset_ids)
        if duplicates:
            raise ValueError(
                f"Initial Metric belongs to multiple candidate sets: {sorted(duplicates)}"
            )
        declared_metric_ids.update(asset_ids)
        compositions.append(
            InitialCandidateComposition(
                candidate_id=candidate_id,
                assets=[metrics[item] for item in asset_ids],
            )
        )
    if declared_metric_ids != set(metrics):
        raise ValueError(
            "Initial Metric candidate sets must cover every Metric exactly once: "
            f"missing={sorted(set(metrics) - declared_metric_ids)}"
        )

    compositions.extend(
        InitialCandidateComposition(
            candidate_id=asset.id,
            assets=[asset],
        )
        for asset in candidate_by_id.values()
        if asset.node_type in {NodeType.SEMANTIC_MAPPING, NodeType.GENERATION_RULE}
    )
    _validate_compositions(compositions)
    return sorted(compositions, key=lambda item: item.candidate_id)


def _validate_candidates(candidates: list[NodeAsset]) -> dict[str, NodeAsset]:
    candidate_by_id = {candidate.id: candidate for candidate in candidates}
    if len(candidate_by_id) != len(candidates):
        raise ValueError("initial candidates contain duplicate asset IDs")
    unsupported = sorted(
        candidate.id
        for candidate in candidates
        if candidate.node_type
        not in {
            NodeType.METRIC,
            NodeType.SEMANTIC_MAPPING,
            NodeType.GENERATION_RULE,
        }
    )
    if unsupported:
        raise ValueError(
            "initial extraction supports only Metric, Semantic Mapping, and Generation Rule: "
            f"{unsupported}"
        )
    return candidate_by_id


def _validate_compositions(
    candidates: list[InitialCandidateComposition],
) -> dict[str, InitialCandidateComposition]:
    candidate_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    if len(candidate_by_id) != len(candidates):
        raise ValueError("initial candidates contain duplicate candidate IDs")
    asset_ids = [asset.id for candidate in candidates for asset in candidate.assets]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("initial candidate compositions overlap")
    _validate_candidates([asset for candidate in candidates for asset in candidate.assets])
    return candidate_by_id
