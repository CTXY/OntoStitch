"""Stable authoring-asset fingerprints for exported ontology collections."""

from __future__ import annotations

import hashlib
import json

from cortex.capabilities.sql_training.models import NodeAsset


def ontology_fingerprint(assets: list[NodeAsset]) -> str:
    payload = [asset.model_dump(mode="json") for asset in sorted(assets, key=lambda row: row.id)]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
