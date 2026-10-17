"""Shared identity contracts for Repair planning, optimization, and assessment."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from cortex.capabilities.sql_training.models import NodeAsset


class OptimizationOwner(StrEnum):
    SEMANTIC_MAPPING = "semantic_mapping"
    METRIC = "metric"
    GENERATION_RULE = "generation_rule"
    NONE = "none"
    INCONCLUSIVE = "inconclusive"


def asset_fingerprint(asset: NodeAsset) -> str:
    payload = json.dumps(
        asset.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ontology_fingerprint(assets: list[NodeAsset]) -> str:
    payload = [asset.model_dump(mode="json") for asset in sorted(assets, key=lambda row: row.id)]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
