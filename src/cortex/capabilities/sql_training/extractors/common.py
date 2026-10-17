from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.models import NodeAsset

SEMANTIC_CANDIDATE_TYPES = {
    NodeType.METRIC,
    NodeType.SEMANTIC_MAPPING,
    NodeType.GENERATION_RULE,
}


@dataclass(frozen=True)
class CandidateSource:
    source_type: SourceType
    source_id: str
    error_prefix: str = "CANDIDATE"


class TypedCandidateExtractor:
    asset_type: NodeType

    def extract(
        self,
        raw_assets: list[dict[str, Any]],
        *,
        source: CandidateSource,
        skipped: list[dict[str, Any]],
    ) -> list[NodeAsset]:
        assets: list[NodeAsset] = []
        for raw_asset in raw_assets:
            try:
                normalized = self._normalize(raw_asset, source)
            except (ValidationError, ValueError) as exc:
                skipped.append(
                    {
                        "code": f"INVALID_{source.error_prefix}_CANDIDATE_SCHEMA",
                        "record": source.source_id,
                        "asset_id": raw_asset.get("id"),
                        "asset_type": raw_asset.get("node_type"),
                        "errors": exc.errors()
                        if isinstance(exc, ValidationError)
                        else [{"type": "value_error", "msg": str(exc)}],
                    }
                )
                continue
            try:
                assets.append(NodeAsset.model_validate(normalized))
            except ValidationError as exc:
                skipped.append(
                    {
                        "code": f"INVALID_{source.error_prefix}_CANDIDATE_SCHEMA",
                        "record": source.source_id,
                        "asset_id": normalized.get("id"),
                        "asset_type": normalized.get("node_type"),
                        "errors": exc.errors(),
                    }
                )
        return assets

    def _normalize(self, raw_asset: dict[str, Any], source: CandidateSource) -> dict[str, Any]:
        normalized = dict(raw_asset)
        normalized.setdefault("sources", [_source_payload(source)])
        return normalized


def normalize_properties_payload(
    raw_asset: dict[str, Any],
    *,
    property_keys: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = dict(raw_asset)
    raw_properties = normalized.get("properties") or {}
    properties = dict(raw_properties) if isinstance(raw_properties, dict) else {}
    for key in property_keys:
        if key in normalized:
            value = normalized.pop(key)
            if key not in properties:
                properties[key] = value
    normalized["properties"] = properties
    return normalized, properties


def ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_typed_candidates(
    raw_assets: list[Any],
    *,
    source: CandidateSource,
    extractors: list[TypedCandidateExtractor],
    skipped: list[dict[str, Any]],
) -> dict[NodeType, list[NodeAsset]]:
    raw_by_type = _partition_raw_assets(raw_assets, source=source, skipped=skipped)
    assets_by_type: dict[NodeType, list[NodeAsset]] = defaultdict(list)
    for extractor in extractors:
        assets = extractor.extract(
            raw_by_type.get(extractor.asset_type, []),
            source=source,
            skipped=skipped,
        )
        if assets:
            assets_by_type[extractor.asset_type].extend(assets)
    return dict(assets_by_type)


def _partition_raw_assets(
    raw_assets: list[Any],
    *,
    source: CandidateSource,
    skipped: list[dict[str, Any]],
) -> dict[NodeType, list[dict[str, Any]]]:
    raw_by_type: dict[NodeType, list[dict[str, Any]]] = defaultdict(list)
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise ValueError(
                f"{source.error_prefix} source {source.source_id} contains non-object asset"
            )
        asset_type = _raw_asset_type(raw_asset, source=source, skipped=skipped)
        if asset_type is None:
            continue
        if asset_type not in SEMANTIC_CANDIDATE_TYPES:
            skipped.append(
                {
                    "code": f"UNSUPPORTED_{source.error_prefix}_CANDIDATE_TYPE",
                    "record": source.source_id,
                    "asset_id": raw_asset.get("id"),
                    "asset_type": asset_type.value,
                }
            )
            continue
        raw_by_type[asset_type].append(raw_asset)
    return dict(raw_by_type)


def _raw_asset_type(
    raw_asset: dict[str, Any],
    *,
    source: CandidateSource,
    skipped: list[dict[str, Any]],
) -> NodeType | None:
    try:
        return NodeType(raw_asset.get("node_type"))
    except ValueError:
        skipped.append(
            {
                "code": f"INVALID_{source.error_prefix}_CANDIDATE_TYPE",
                "record": source.source_id,
                "asset_id": raw_asset.get("id"),
                "asset_type": raw_asset.get("node_type"),
            }
        )
        return None


def _source_payload(source: CandidateSource) -> dict[str, Any]:
    return {
        "source_type": source.source_type,
        "source_id": source.source_id,
    }
