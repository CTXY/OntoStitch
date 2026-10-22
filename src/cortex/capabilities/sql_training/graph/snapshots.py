"""Immutable authoring snapshots; GraphSpace is a projection, never the edit source."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.contracts.references import (
    validate_asset_batch_references,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.adapter import NodeGraphAdapter
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.schema import GraphEdge, GraphSpace, SchemaDefinition

SEMANTIC_TYPES = frozenset(
    {NodeType.CONCEPT, NodeType.METRIC, NodeType.GENERATION_RULE}
)
PHYSICAL_TYPES = frozenset({NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE})


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


class GraphSnapshot(NodeModel):
    revision: str
    parent_revision: str | None = None
    schema_version: str
    content_fingerprint: str
    assets: list[NodeAsset]
    catalog_edges: list[GraphEdge] = Field(default_factory=list)

    @property
    def semantic_assets(self) -> list[NodeAsset]:
        return [
            a.model_copy(deep=True)
            for a in self.assets
            if a.node_type in SEMANTIC_TYPES
        ]

    @property
    def physical_assets(self) -> list[NodeAsset]:
        return [
            a.model_copy(deep=True)
            for a in self.assets
            if a.node_type in PHYSICAL_TYPES
        ]


def table_schema(assets: list[NodeAsset]) -> dict[str, str]:
    return {
        a.properties.table_name: a.properties.table_schema
        for a in assets
        if a.node_type == NodeType.TABLE
    }


class GraphStore:
    """One writer and two revision pointers backed by complete immutable JSON files.

    The controller checkpoints the pointers at safe boundaries. A saved edit alone
    is not an accepted checkpoint and is allowed to be discarded after a crash.
    """

    def __init__(self, root: Path, *, workspace: Workspace, schema: SchemaDefinition):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace
        self.schema = schema
        self.schema_version = fingerprint(schema.model_dump(mode="json"))
        self.working_revision = ""
        self.accepted_revision = ""
        self._physical: dict[str, NodeAsset] = {}
        self._catalog_edges: list[GraphEdge] = []

    def initialize(
        self, assets: list[NodeAsset], catalog_edges: list[GraphEdge]
    ) -> GraphSnapshot:
        if self.working_revision:
            raise ValueError("graph store is already initialized")
        self._catalog_edges = list(catalog_edges)
        snapshot = self.save(assets, parent_revision=None)
        self._physical = {a.id: a for a in snapshot.physical_assets}
        self.working_revision = self.accepted_revision = snapshot.revision
        return snapshot

    def read(self, revision: str) -> GraphSnapshot:
        if not revision or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("invalid graph revision")
        payload = read_json(self.root / f"{revision}.json")
        if payload is None:
            raise ValueError(f"unknown graph revision: {revision}")
        snapshot = GraphSnapshot.model_validate(payload)
        if snapshot.schema_version != self.schema_version:
            raise ValueError("snapshot ontology schema version changed")
        if snapshot.content_fingerprint != self._content_hash(
            snapshot.assets, snapshot.catalog_edges
        ):
            raise ValueError("snapshot content fingerprint mismatch")
        return snapshot

    @property
    def working(self) -> GraphSnapshot:
        return self.read(self.working_revision)

    @property
    def accepted(self) -> GraphSnapshot:
        return self.read(self.accepted_revision)

    def restore(self, working_revision: str, accepted_revision: str) -> None:
        working, accepted = self.read(working_revision), self.read(accepted_revision)
        if working.catalog_edges != accepted.catalog_edges:
            raise ValueError("checkpoint physical topology mismatch")
        self._catalog_edges = working.catalog_edges
        self._physical = {
            a.id: a for a in [*accepted.physical_assets, *working.physical_assets]
        }
        self.working_revision, self.accepted_revision = (
            working_revision,
            accepted_revision,
        )

    def validation_graph(self, revision: str) -> GraphSpace:
        snap = self.read(revision)
        return self.validate(snap.assets, snap.catalog_edges)

    def validate(
        self, assets: list[NodeAsset], catalog_edges: list[GraphEdge]
    ) -> GraphSpace:
        # model_copy(update=...) bypasses validation; always parse the complete boundary.
        assets = [NodeAsset.model_validate(a.model_dump(mode="json")) for a in assets]
        by_id = {a.id: a for a in assets}
        if len(by_id) != len(assets):
            raise ValueError("duplicate graph asset IDs")
        if any(a.node_type not in SEMANTIC_TYPES | PHYSICAL_TYPES for a in assets):
            raise ValueError("case graph only accepts semantic and physical assets")
        if not any(a.node_type == NodeType.TABLE for a in assets) or not any(
            a.node_type == NodeType.COLUMN for a in assets
        ):
            raise ValueError("case graph requires all configured Tables and Columns")
        semantic = [a for a in assets if a.node_type in SEMANTIC_TYPES]
        validate_asset_batch_references(
            semantic, working_assets=[], table_schema=table_schema(assets)
        )
        missing = sorted(
            {
                ref
                for a in semantic
                for ref in a.references()
                if ref.startswith("dimension_value.") and ref not in by_id
            }
        )
        if missing:
            raise ValueError(f"unverified Dimension Value references: {missing}")
        graph, warnings = NodeGraphAdapter(self.workspace).to_authoring_graph(
            self.schema,
            assets=assets,
            catalog_edges=catalog_edges,
        )
        if warnings:
            raise ValueError(f"incomplete graph projection: {list(warnings)}")
        return graph

    def save(
        self, assets: list[NodeAsset], *, parent_revision: str | None
    ) -> GraphSnapshot:
        normalized = [
            NodeAsset.model_validate(a.model_dump(mode="json")) for a in assets
        ]
        normalized.sort(key=lambda a: a.id)
        self.validate(normalized, self._catalog_edges)
        snap = GraphSnapshot(
            revision=uuid4().hex,
            parent_revision=parent_revision,
            schema_version=self.schema_version,
            content_fingerprint=self._content_hash(normalized, self._catalog_edges),
            assets=normalized,
            catalog_edges=self._catalog_edges,
        )
        write_json_atomic(
            self.root / f"{snap.revision}.json", snap.model_dump(mode="json")
        )
        return snap

    def publish_edit(self, assets: list[NodeAsset]) -> GraphSnapshot:
        snap = self.save(assets, parent_revision=self.working_revision)
        self._physical = {a.id: a for a in snap.physical_assets}
        self.working_revision = snap.revision
        return snap

    def retain_direct(self) -> None:
        self.accepted_revision = self.working_revision

    def promote(
        self,
        *,
        assessed_revision: str,
        accepted_before: str,
        member_ids: list[str],
        publish: bool = True,
    ) -> GraphSnapshot:
        if self.accepted_revision != accepted_before:
            raise ValueError("assessment accepted_revision_before is stale")
        if not member_ids:
            return self.accepted
        assessed, old = self.read(assessed_revision), self.accepted
        proposed = {a.id: a for a in assessed.semantic_assets}
        previous = {a.id: a for a in old.semantic_assets}
        changed = set(semantic_diff(old, assessed))
        selected = set(member_ids)
        if not selected <= changed:
            raise ValueError(
                "acceptance members must belong to the assessed tentative diff"
            )
        for node_id in selected:
            if node_id not in proposed:
                raise ValueError("retained semantic deletion is not supported")
            if (set(proposed[node_id].references()) & changed) - selected:
                raise ValueError(
                    "accepted group depends on unaccepted tentative content"
                )
        previous.update({key: proposed[key] for key in selected})
        snap = self.save(
            [*previous.values(), *self._physical.values()],
            parent_revision=accepted_before,
        )
        if publish:
            self.accepted_revision = snap.revision
        return snap

    def retain(self) -> GraphSnapshot:
        assets = [*self.accepted.semantic_assets, *self._physical.values()]
        if (
            self._content_hash(assets, self._catalog_edges)
            == self.accepted.content_fingerprint
        ):
            snap = self.accepted
        else:
            snap = self.save(assets, parent_revision=self.accepted_revision)
            self.accepted_revision = snap.revision
        self.working_revision = snap.revision
        return snap

    def _content_hash(self, assets: list[NodeAsset], edges: list[GraphEdge]) -> str:
        return fingerprint(
            {
                "assets": [
                    a.model_dump(mode="json")
                    for a in sorted(assets, key=lambda a: a.id)
                ],
                "edges": [
                    e.model_dump(mode="json") for e in sorted(edges, key=lambda e: e.id)
                ],
                "schema": self.schema_version,
            }
        )


def semantic_diff(before: GraphSnapshot, after: GraphSnapshot) -> list[str]:
    old = {a.id: a.model_dump(mode="json") for a in before.semantic_assets}
    new = {a.id: a.model_dump(mode="json") for a in after.semantic_assets}
    return sorted(
        key for key in old.keys() | new.keys() if old.get(key) != new.get(key)
    )
