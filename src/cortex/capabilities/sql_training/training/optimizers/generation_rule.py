from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.training.optimizers.base import (
    BaseAssetOptimizer,
    _base_retry_instruction,
)
from cortex.capabilities.sql_training.training.repair.contracts import OptimizationOwner
from cortex.capabilities.sql_training.training.repair.plan import CandidateInitializationResult

_TRAINING_CONTRACT_RE = re.compile(r"\bevaluation contract\b", re.IGNORECASE)


class GenerationRuleOptimizer(BaseAssetOptimizer):
    """Create or revise reusable SQL-generation behavior rules."""

    def __init__(self, provider: Any):
        super().__init__(
            provider,
            owner=OptimizationOwner.GENERATION_RULE,
            prompt_name="train_residual_generation_rule_optimizer",
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
                raise ValueError("Generation Rule update changed the accepted rule name")

    def validate_refinement_identity(
        self,
        original: NodeAsset,
        replacement: NodeAsset,
    ) -> None:
        if replacement.name != original.name:
            raise ValueError("Generation Rule refinement changed the rule name")

    def validate_assets(
        self,
        assets: list[NodeAsset],
        *,
        working_assets: list[NodeAsset],
        evidence: dict[str, Any],
    ) -> None:
        del working_assets, evidence
        for asset in assets:
            _validate_residual_rule_boundary(asset)

    def retry_instruction(self, error: str, *, refinement: bool = False) -> str:
        base = _base_retry_instruction(error, refinement=refinement)
        if "training-only evaluation contract" in error:
            return (
                f"{base} Rewrite the asset content from the observable business behavior. "
                "Do not mention an evaluation contract, required-column contract, training "
                "case, gold output, expected result, or validation requirement anywhere in the "
                "name, description, condition, action, or reason. Express only what a runtime "
                "Question makes selectable and what SQL behavior must follow."
            )
        return (
            f"{base} State a reusable conditional SQL behavior using semantic roles; remove "
            "training/evaluation language, fixed output aliases, case literals, and full query "
            "templates."
        )


def _validate_residual_rule_boundary(asset: NodeAsset) -> None:
    properties = asset.properties
    text = " ".join(
        [
            asset.name,
            asset.description or "",
            properties.condition or "",
            properties.action or "",
            properties.reason or "",
        ]
    )
    if _TRAINING_CONTRACT_RE.search(text):
        raise ValueError("Generation Rule contains a training-only evaluation contract")


def matched_ontology_ids(intent_output: Any) -> set[str]:
    """Normalize ontology IDs selected by a successful intent call."""

    if isinstance(intent_output, BaseModel):
        intent_output = intent_output.model_dump(mode="json")
    if not isinstance(intent_output, dict):
        raise ValueError("ontology intent output is unusable")
    data = intent_output.get("data")
    if isinstance(data, dict) and "matched_ontology" in data:
        raw_items = data["matched_ontology"]
    else:
        packet = _ontology_packet(intent_output)
        if not isinstance(packet, dict):
            raise ValueError("ontology intent output has no ontology packet")
        if "matched_ontology" in packet:
            raw_items = packet["matched_ontology"]
        elif "matched_terms" in packet:
            raw_items = packet["matched_terms"]
        else:
            raise ValueError("ontology intent output has no matched ontology collection")
    if not isinstance(raw_items, list):
        raise ValueError("matched ontology collection must be a list")
    matched: set[str] = set()
    for item in raw_items:
        if isinstance(item, str) and item.strip():
            matched.add(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        for key in ("id", "node_id", "asset_id"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                matched.add(value.strip())
        for key in ("refs", "node_ids"):
            values = item.get(key)
            if isinstance(values, list):
                matched.update(
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                )
    return matched


def _ontology_packet(intent_output: dict[str, Any]) -> Any:
    data = intent_output.get("data")
    if isinstance(data, dict) and "ontology_packet" in data:
        return data.get("ontology_packet")
    if "ontology_packet" in intent_output:
        return intent_output.get("ontology_packet")
    return intent_output
