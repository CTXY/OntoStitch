"""Small, serial graph mutation boundary shared by Learning tools and workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from cortex.capabilities.sql_training.core.enums import NodeType, SourceType
from cortex.capabilities.sql_training.graph.snapshots import (
    SEMANTIC_TYPES,
    GraphSnapshot,
    GraphStore,
    semantic_diff,
    table_schema,
)
from cortex.capabilities.sql_training.graph.values import merge_verified_values
from cortex.capabilities.sql_training.models import NodeAsset, Source
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.capabilities.sql_training.pipeline.grounding.semantic_values import (
    materialize_semantic_mapping_dimension_values,
)
from cortex.infrastructure.database import DatabaseConnector


class ChangeGroup(NodeModel):
    group_id: str = Field(min_length=1)
    member_ids: list[str] = Field(min_length=1)
    target_ids: list[str] = Field(default_factory=list)


class GraphOperation(NodeModel):
    operation: Literal["create", "update", "revert"]
    node_id: str
    asset: NodeAsset | None = None

    @model_validator(mode="after")
    def check_payload(self):
        if self.operation == "revert":
            if self.asset is not None:
                raise ValueError("revert takes node_id only")
        elif self.asset is None or self.asset.id != self.node_id:
            raise ValueError(
                "create/update requires the full asset with matching node_id"
            )
        return self


class GraphEdit(NodeModel):
    edit_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: str
    operations: list[GraphOperation] = Field(min_length=1, max_length=50)
    proposed_groups: list[ChangeGroup] = Field(default_factory=list)


def apply_edit(
    store: GraphStore, edit: GraphEdit, *, database: DatabaseConnector, source_id: str
) -> GraphSnapshot:
    if edit.expected_revision != store.working_revision:
        raise ValueError(f"stale expected_revision; current={store.working_revision}")
    current, accepted = store.working, store.accepted
    by_id = {a.id: a for a in current.assets}
    retained = {a.id: a for a in accepted.assets}
    seen: set[str] = set()
    for op in edit.operations:
        if op.node_id in seen:
            raise ValueError("one operation per node in an edit")
        seen.add(op.node_id)
        existing = by_id.get(op.node_id)
        if op.operation == "revert":
            if existing is None or existing.node_type not in SEMANTIC_TYPES:
                raise ValueError("only tentative semantic content can be reverted")
            if op.node_id in retained:
                by_id[op.node_id] = retained[op.node_id]
            else:
                del by_id[op.node_id]
            continue
        asset = op.asset
        if asset.node_type not in SEMANTIC_TYPES:
            raise ValueError("Learning Agent cannot create or edit physical assets")
        if op.operation == "create" and existing is not None:
            raise ValueError("create requires a new ID")
        if op.operation == "update" and existing is None:
            raise ValueError("update requires an existing ID")
        if existing is not None and existing.node_type != asset.node_type:
            raise ValueError("node type and identity are immutable")
        # Attach trustworthy case provenance instead of accepting model-written sources.
        asset = asset.model_copy(
            update={
                "sources": [
                    *(existing.sources if existing else []),
                    Source(source_type=SourceType.QA, source_id=source_id),
                ]
            }
        )
        unique = {(s.source_type, s.source_id): s for s in asset.sources}
        by_id[asset.id] = asset.model_copy(update={"sources": list(unique.values())})
    semantics = [
        by_id[op.node_id] for op in edit.operations if op.operation != "revert"
    ]
    semantics, values = materialize_semantic_mapping_dimension_values(
        semantics,
        table_schema=table_schema(current.assets),
        database=database,
        source_id=source_id,
    )
    values = merge_verified_values(
        [a for a in by_id.values() if a.node_type == NodeType.DIMENSION_VALUE], values
    )
    by_id.update({a.id: a for a in [*semantics, *values]})
    snap = store.save(list(by_id.values()), parent_revision=current.revision)
    store.restore(snap.revision, store.accepted_revision)
    return snap


def normalize_groups(
    groups: list[ChangeGroup], before: GraphSnapshot, after: GraphSnapshot
) -> list[ChangeGroup]:
    """Merge overlapping targets/nodes and explicit tentative references; no subset solver."""
    changed = set(semantic_diff(before, after))
    pending: list[ChangeGroup] = []
    for group in groups:
        members = sorted(set(group.member_ids) & changed)
        if members:
            pending.append(group.model_copy(update={"member_ids": members}))
    uncovered = changed - {key for group in pending for key in group.member_ids}
    if uncovered:
        pending.append(ChangeGroup(group_id="unassigned", member_ids=sorted(uncovered)))
    by_id = {a.id: a for a in after.semantic_assets}
    merged = True
    while merged:
        merged = False
        for i, left in enumerate(pending):
            left_ids = set(left.member_ids)
            left_refs = {
                ref
                for key in left_ids
                if key in by_id
                for ref in by_id[key].references()
            }
            for j in range(i + 1, len(pending)):
                right = pending[j]
                right_ids = set(right.member_ids)
                right_refs = {
                    ref
                    for key in right_ids
                    if key in by_id
                    for ref in by_id[key].references()
                }
                if (
                    left_ids & right_ids
                    or set(left.target_ids) & set(right.target_ids)
                    or left_refs & right_ids
                    or right_refs & left_ids
                ):
                    pending[i] = ChangeGroup(
                        group_id=left.group_id,
                        member_ids=sorted(left_ids | right_ids),
                        target_ids=sorted(set(left.target_ids) | set(right.target_ids)),
                    )
                    del pending[j]
                    merged = True
                    break
            if merged:
                break
    # Stable IDs are local to this assessed tentative diff, never long-lived identities.
    return [
        g.model_copy(update={"group_id": f"g{n}"}) for n, g in enumerate(pending, 1)
    ]
