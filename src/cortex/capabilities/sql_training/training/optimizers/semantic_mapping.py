from __future__ import annotations

import re
from typing import Any

from cortex.capabilities.sql_training.contracts.semantic_mapping import (
    normalize_semantic_mapping_candidate,
    validate_semantic_mapping_candidate_contract,
    validate_semantic_mapping_schema_grounding,
)
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.optimizers.base import (
    BaseAssetOptimizer,
    _base_retry_instruction,
)
from cortex.capabilities.sql_training.training.repair.contracts import OptimizationOwner
from cortex.capabilities.sql_training.training.repair.plan import CandidateInitializationResult

_SQL_BEHAVIOR_RE = re.compile(
    r"\b(?:select|group\s+by|order\s+by|join|top\s*[- ]?n|rank|aggregate|projection)\b",
    re.IGNORECASE,
)


class SemanticMappingOptimizer(BaseAssetOptimizer):
    """Create or revise reusable concept-to-physical-value mappings."""

    def __init__(self, provider: Any):
        super().__init__(
            provider,
            owner=OptimizationOwner.SEMANTIC_MAPPING,
            prompt_name="train_residual_concept_optimizer",
        )

    def validate_initialization_identity(
        self,
        base_asset: NodeAsset | None,
        result: CandidateInitializationResult,
    ) -> None:
        if base_asset is None:
            return
        for candidate in result.candidates:
            if candidate.asset.name != base_asset.name:
                raise ValueError("Semantic Mapping update changed the accepted concept name")

    def validate_refinement_identity(
        self,
        original: NodeAsset,
        replacement: NodeAsset,
    ) -> None:
        if replacement.name != original.name:
            raise ValueError("Semantic Mapping refinement changed the concept name")

    def validate_assets(
        self,
        assets: list[NodeAsset],
        *,
        working_assets: list[NodeAsset],
        evidence: dict[str, Any],
    ) -> None:
        del working_assets
        table_schema = evidence.get("table_schema")
        for asset in assets:
            self._validate_behavior_boundary(asset)
            normalized = normalize_semantic_mapping_candidate(
                asset,
                table_schema=table_schema if isinstance(table_schema, dict) else {},
            )
            asset.properties = normalized.properties
            validate_semantic_mapping_candidate_contract(
                asset,
                table_schema=table_schema if isinstance(table_schema, dict) else {},
            )
            if isinstance(table_schema, dict):
                validate_semantic_mapping_schema_grounding(
                    asset,
                    table_schema=table_schema,
                )

    def retry_instruction(self, error: str, *, refinement: bool = False) -> str:
        base = _base_retry_instruction(error, refinement=refinement)
        if "multiple Semantic Mapping endpoints" in error:
            return (
                f"{base} Re-evaluate endpoint ownership: use one_of only for alternative "
                "endpoints, all_of only when all endpoints jointly define one concept, and "
                "create independent concepts when the meanings differ."
            )
        return (
            f"{base} Keep only reusable concept identity, aliases, and grounded value-filter "
            "bindings; remove SQL-generation instructions."
        )

    @staticmethod
    def _validate_behavior_boundary(asset: NodeAsset) -> None:
        text = " ".join([asset.description or "", *asset.aliases])
        if _SQL_BEHAVIOR_RE.search(text):
            raise ValueError(
                "Semantic Mapping contains SQL-generation behavior in description or aliases"
            )
