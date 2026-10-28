from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    read_jsonl,
    write_json_atomic,
    write_jsonl,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.schema import GraphEdge

ASSET_GROUP_FILES: dict[NodeType, str] = {
    NodeType.TABLE: "tables.json",
    NodeType.COLUMN: "columns.json",
    NodeType.DIMENSION_VALUE: "dimension_values.json",
    NodeType.METRIC: "metrics.json",
    NodeType.CONCEPT: "concepts.json",
    NodeType.GENERATION_RULE: "generation_rules.json",
    NodeType.EXAMPLE: "examples.json",
}

CANDIDATE_GROUP_FILES: dict[NodeType, str] = {
    NodeType.METRIC: "metrics.candidates.jsonl",
    NodeType.CONCEPT: "concepts.candidates.jsonl",
    NodeType.GENERATION_RULE: "generation_rules.candidates.jsonl",
    NodeType.EXAMPLE: "examples.candidates.jsonl",
    NodeType.DIMENSION_VALUE: "dimension_values.candidates.jsonl",
    NodeType.TABLE: "tables.candidates.jsonl",
    NodeType.COLUMN: "columns.candidates.jsonl",
}


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def draft_dir(self) -> Path:
        return self.root / "assets" / "draft"

    @property
    def candidates_dir(self) -> Path:
        return self.root / "candidates"

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def source_evidence_path(self) -> Path:
        return self.candidates_dir / "source_evidence.jsonl"

    def init(self) -> None:
        for path in [
            self.inputs_dir,
            self.draft_dir,
            self.candidates_dir,
            self.root / "logs",
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def write_draft_assets(
        self, asset_type: NodeType, assets: Iterable[NodeAsset]
    ) -> Path:
        self.init()
        path = self.draft_dir / ASSET_GROUP_FILES[asset_type]
        return write_json_atomic(
            path, [_serialize_asset_row(asset) for asset in assets]
        )

    def write_catalog_edges(self, edges: Iterable[GraphEdge]) -> Path:
        """Persist physical relationships as edges, never as node assets."""

        self.init()
        return write_json_atomic(
            self.draft_dir / "catalog_edges.json",
            [edge.model_dump(mode="json") for edge in edges],
        )

    def read_catalog_edges(self) -> list[GraphEdge]:
        rows = read_json(self.draft_dir / "catalog_edges.json", default=[]) or []
        return [GraphEdge.model_validate(row) for row in rows]

    def read_draft_assets(self, asset_type: NodeType) -> list[NodeAsset]:
        path = self.draft_dir / ASSET_GROUP_FILES[asset_type]
        rows = read_json(path, default=[]) or []
        return [_parse_asset_row(row, expected_type=asset_type) for row in rows]

    def delete_draft_assets(self, asset_type: NodeType) -> None:
        path = self.draft_dir / ASSET_GROUP_FILES[asset_type]
        path.unlink(missing_ok=True)

    def append_candidates(
        self, asset_type: NodeType, assets: Iterable[NodeAsset]
    ) -> Path:
        self.init()
        path = self.candidates_dir / CANDIDATE_GROUP_FILES[asset_type]
        return write_jsonl(
            path, [_serialize_asset_row(asset) for asset in assets], append=True
        )

    def write_candidates(
        self, asset_type: NodeType, assets: Iterable[NodeAsset]
    ) -> Path:
        self.init()
        path = self.candidates_dir / CANDIDATE_GROUP_FILES[asset_type]
        return write_jsonl(path, [_serialize_asset_row(asset) for asset in assets])

    def read_candidates(self, asset_type: NodeType) -> list[NodeAsset]:
        path = self.candidates_dir / CANDIDATE_GROUP_FILES[asset_type]
        return [
            _parse_asset_row(row, expected_type=asset_type) for row in read_jsonl(path)
        ]

    def write_source_evidence(self, rows: Iterable[dict[str, Any]]) -> Path:
        self.init()
        return write_jsonl(self.source_evidence_path(), list(rows))

    def upsert_source_evidence(self, rows: Iterable[dict[str, Any]]) -> Path:
        existing_by_id = {
            str(row["source_id"]): row
            for row in read_jsonl(self.source_evidence_path())
            if row.get("source_id")
        }
        for row in rows:
            existing_by_id[str(row["source_id"])] = row
        return self.write_source_evidence(existing_by_id.values())

    def read_source_evidence(self) -> list[dict[str, Any]]:
        return read_jsonl(self.source_evidence_path())

    def read_all_draft_assets(self) -> list[NodeAsset]:
        assets: list[NodeAsset] = []
        for asset_type in NodeType:
            assets.extend(self.read_draft_assets(asset_type))
        return assets

    def read_committed_assets(self) -> list[NodeAsset]:
        """Read the canonical assets visible in the permanent training workspace.

        Case-local semantic outputs and candidate files are intentionally outside this API.
        Stable case outputs enter Graph Merge through graph generation and GraphManager after Train returns;
        this method contains only permanent workspace assets.
        """
        return self.read_all_draft_assets()

    def read_all_candidates(self) -> list[NodeAsset]:
        assets: list[NodeAsset] = []
        for asset_type in NodeType:
            assets.extend(self.read_candidates(asset_type))
        return assets


def _parse_asset_row(row: Any, *, expected_type: NodeType) -> NodeAsset:
    asset = NodeAsset.model_validate(row)
    if asset.node_type != expected_type:
        raise ValueError(
            f"Asset node_type {asset.node_type.value!r} does not match "
            f"file type {expected_type.value!r}"
        )
    return asset


def serialize_asset_envelope(asset: NodeAsset) -> dict[str, Any]:
    """Serialize the canonical chenyu asset envelope without a compatibility projection."""
    return asset.model_dump(mode="json")


def deserialize_asset_envelope(row: Any) -> NodeAsset:
    """Validate one canonical ontology document without reshaping it."""

    return NodeAsset.model_validate(row)


# Kept private as an implementation alias for the workspace write paths below.
_serialize_asset_row = serialize_asset_envelope
