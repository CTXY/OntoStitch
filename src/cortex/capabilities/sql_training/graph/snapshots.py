"""Immutable node definitions and explicit relations with a validated graph projection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal
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
from cortex.capabilities.sql_training.graph.nodes import build_graph, build_node
from cortex.capabilities.sql_training.models import NodeAsset
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    SchemaDefinition,
)
from cortex.schema.relationships import is_dependency_edge

SEMANTIC_TYPES = frozenset(
    {NodeType.CONCEPT, NodeType.METRIC, NodeType.GENERATION_RULE}
)
PHYSICAL_TYPES = frozenset({NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE})
EDITABLE_TYPES = SEMANTIC_TYPES | {NodeType.DIMENSION_VALUE}


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
    format_version: Literal["inline-node-conflicts-v1"] = "inline-node-conflicts-v1"
    revision: str
    parent_revision: str | None = None
    schema_version: str
    content_fingerprint: str
    assets: list[NodeAsset]
    catalog_edges: list[GraphEdge] = Field(default_factory=list)
    explicit_edges: list[GraphEdge] = Field(default_factory=list)
    graph: GraphSpace
    graph_fingerprint: str

    @property
    def conflict_nodes(self) -> tuple[GraphNode, ...]:
        return tuple(node for node in self.graph.nodes if node.alternatives)

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

    @property
    def editable_assets(self) -> list[NodeAsset]:
        return [
            a.model_copy(deep=True)
            for a in self.assets
            if a.node_type in EDITABLE_TYPES
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
        if any(a.node_type not in PHYSICAL_TYPES for a in assets):
            raise ValueError("case initialization accepts physical facts only")
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
        if payload.get("format_version") != "inline-node-conflicts-v1":
            raise ValueError("snapshot graph format changed; start a new training case")
        snapshot = GraphSnapshot.model_validate(payload)
        if snapshot.schema_version != self.schema_version:
            raise ValueError("snapshot ontology schema version changed")
        if snapshot.content_fingerprint != self._content_hash(
            snapshot.assets,
            snapshot.catalog_edges,
            snapshot.explicit_edges,
            snapshot.graph,
        ):
            raise ValueError("snapshot content fingerprint mismatch")
        if snapshot.graph_fingerprint != fingerprint(
            snapshot.graph.model_dump(mode="json")
        ):
            raise ValueError("snapshot graph fingerprint mismatch")
        from cortex.application.ontology_graph import GraphManager

        GraphManager(self.schema).validate_state(
            GraphState(graph=snapshot.graph)
        )
        conflict_ids = {c.id for c in snapshot.conflict_nodes}
        if {n.id: n for n in snapshot.graph.nodes if n.id not in conflict_ids} != {
            a.id: build_node(a) for a in snapshot.assets
        }:
            raise ValueError("snapshot nodes disagree with asset definitions")
        saved_relations = {
            e.id: e for e in (*snapshot.catalog_edges, *snapshot.explicit_edges)
        }
        if saved_relations != {
            e.id: e for e in snapshot.graph.edges if e.reference_origin != "default"
        }:
            raise ValueError("snapshot relations disagree with authored relations")
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

    def manager(self, revision: str | None = None):
        from cortex.application.ontology_graph import GraphManager

        selected = revision or self.working_revision
        return GraphManager(
            self.schema,
            GraphState(
                graph=self.read(selected).graph,
            ),
            revision=selected,
        )

    def validation_graph(self, revision: str) -> GraphSpace:
        snap = self.read(revision)
        return snap.graph.model_copy(deep=True)

    def validate(
        self,
        assets: list[NodeAsset],
        catalog_edges: list[GraphEdge],
        explicit_edges: list[GraphEdge] | None = None,
        state: GraphState | None = None,
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
        if state is not None:
            from cortex.application.ontology_graph import GraphManager

            by_node = {n.id: n for n in state.graph.nodes}
            for conflict in state.conflict_nodes:
                owner = by_node[conflict.id]
                for alternative in conflict.alternatives:
                    content = GraphManager.resolve_alternative(owner, alternative)
                    semantic.append(
                        semantic_asset(
                            owner.model_copy(
                                update={
                                    **content.model_dump(mode="json"),
                                    "sources": alternative.sources,
                                }
                            )
                        )
                    )
        if state is None:
            validate_asset_batch_references(
                semantic,
                working_assets=[a for a in assets if a.node_type in PHYSICAL_TYPES],
                table_schema=table_schema(assets),
            )
        else:
            # Validate each authored branch without treating mutually exclusive
            # candidates as duplicate nodes or deriving a second dependency graph.
            from cortex.capabilities.sql_training.contracts.references import (
                _validate_asset_references,
                physical_reference_catalog,
            )

            physical_ids = physical_reference_catalog(
                table_schema(assets),
                dimension_values=[
                    a for a in assets if a.node_type == NodeType.DIMENSION_VALUE
                ],
            )
            for asset in semantic:
                _validate_asset_references(
                    asset,
                    visible_ids={n.id for n in state.graph.nodes},
                    physical_ids=physical_ids,
                    table_schema=table_schema(assets),
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
        if state is not None:
            from cortex.application.ontology_graph import GraphManager

            GraphManager(self.schema).validate_state(state)
            return state.graph
        graph = build_graph(
            assets, catalog_edges, self.schema, explicit_edges=explicit_edges or []
        )
        return graph

    def save(
        self,
        assets: list[NodeAsset],
        *,
        parent_revision: str | None,
        explicit_edges: list[GraphEdge] | None = None,
        state: GraphState | None = None,
    ) -> GraphSnapshot:
        parent = self.read(parent_revision) if parent_revision else None
        if explicit_edges is None:
            explicit_edges = (
                self.read(parent_revision).explicit_edges if parent_revision else []
            )
        explicit_edges = sorted(
            [
                GraphEdge.model_validate(e.model_dump(mode="json"))
                for e in explicit_edges
            ],
            key=lambda e: e.id,
        )
        from cortex.application.ontology_graph import GraphManager

        if state is None and parent and parent.conflict_nodes:
            conflict_ids = {c.id for c in parent.conflict_nodes}
            if conflict_ids & {a.id for a in assets}:
                raise ValueError(
                    "Conflict nodes must be changed through set_conflict or resolve_conflict"
                )
            state, _ = GraphManager(self.schema).prepare_state(
                GraphState(
                    graph=GraphSpace(
                        nodes=tuple(build_node(a) for a in assets)
                        + tuple(n for n in parent.graph.nodes if n.id in conflict_ids),
                        edges=(*explicit_edges, *self._catalog_edges),
                    ),
                )
            )
        if state is not None:
            state = GraphManager(self.schema).validate_state(state)
            conflict_ids = {c.id for c in state.conflict_nodes}
            by_asset = {a.id: a for a in assets}
            for node in state.graph.nodes:
                if node.id in conflict_ids:
                    by_asset.pop(node.id, None)
                elif node.id not in by_asset or build_node(by_asset[node.id]) != node:
                    by_asset[node.id] = semantic_asset(node)
            assets = list(by_asset.values())
        normalized = [
            NodeAsset.model_validate(a.model_dump(mode="json")) for a in assets
        ]
        normalized.sort(key=lambda a: a.id)
        if state is not None:
            by_asset = {a.id: a for a in normalized}
            state = state.model_copy(
                update={
                    "graph": GraphSpace(
                        nodes=tuple(
                            build_node(by_asset[n.id]) if n.id in by_asset else n
                            for n in state.graph.nodes
                        ),
                        edges=state.graph.edges,
                    )
                }
            )
        graph = self.validate(
            normalized, self._catalog_edges, explicit_edges, state=state
        )
        # Persist canonical overrides (including inferred scope) and discard stale
        # overrides removed with their anchor. Reads never reconstruct relations.
        explicit_ids = {e.id for e in explicit_edges}
        explicit_edges = sorted(
            [e for e in graph.edges if e.id in explicit_ids], key=lambda e: e.id
        )
        snap = GraphSnapshot(
            revision=uuid4().hex,
            parent_revision=parent_revision,
            schema_version=self.schema_version,
            content_fingerprint=self._content_hash(
                normalized, self._catalog_edges, explicit_edges, graph
            ),
            assets=normalized,
            catalog_edges=self._catalog_edges,
            explicit_edges=explicit_edges,
            graph=graph,
            graph_fingerprint=fingerprint(graph.model_dump(mode="json")),
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

    def select(
        self,
        *,
        assessed_revision: str,
        accepted_before: str,
        member_ids: list[str],
    ) -> GraphSnapshot:
        """Build a filtered candidate without advancing either graph head."""
        if self.accepted_revision != accepted_before:
            raise ValueError("assessment accepted_revision_before is stale")
        if not member_ids:
            return self.accepted
        assessed, old = self.read(assessed_revision), self.accepted
        proposed = {a.id: a for a in assessed.editable_assets}
        previous = {a.id: a for a in old.editable_assets}
        changed = set(graph_diff(old, assessed))
        selected = set(member_ids)
        if not selected <= changed:
            raise ValueError(
                "acceptance members must belong to the assessed tentative diff"
            )
        old_edges = {e.id: e for e in old.explicit_edges}
        proposed_edges = {e.id: e for e in assessed.explicit_edges}
        editable_kinds = {kind.value for kind in EDITABLE_TYPES}
        proposed_nodes = {
            n.id: n for n in assessed.graph.nodes if n.node_type in editable_kinds
        }
        old_nodes = {n.id: n for n in old.graph.nodes if n.node_type in editable_kinds}
        node_members = selected & (old_nodes.keys() | proposed_nodes.keys())
        for node_id in node_members:
            if node_id not in proposed_nodes:
                raise ValueError("retained semantic deletion is not supported")
            if (
                {
                    e.to_node_id
                    for e in assessed.graph.edges
                    if e.from_node_id == node_id and is_dependency_edge(e)
                }
                & changed
            ) - selected:
                raise ValueError(
                    "accepted group depends on unaccepted tentative content"
                )
        for key in node_members:
            previous.pop(key, None)
            if key in proposed:
                previous[key] = proposed[key]
        conflicts = {
            c.id: c for c in old.conflict_nodes if c.id not in node_members
        }
        conflicts.update(
            {c.id: c for c in assessed.conflict_nodes if c.id in node_members}
        )
        common = {**old_nodes, **{key: proposed_nodes[key] for key in node_members}}

        for edge_id in selected - node_members:
            if edge_id in proposed_edges:
                old_edges[edge_id] = proposed_edges[edge_id]
            else:
                old_edges.pop(edge_id, None)
        for edge in old_edges.values():
            if (
                edge.id in selected
                and ({edge.from_node_id, edge.to_node_id} & changed) - selected
            ):
                raise ValueError(
                    "accepted relation depends on unaccepted tentative content"
                )
        assets = [
            *previous.values(),
            *[a for a in old.assets if a.node_type not in EDITABLE_TYPES],
        ]
        from cortex.application.ontology_graph import GraphManager

        state, _ = GraphManager(self.schema).prepare_state(
            GraphState(
                graph=GraphSpace(
                    nodes=tuple(build_node(a) for a in assets)
                    + tuple(common[key] for key in conflicts),
                    edges=(*old_edges.values(), *self._catalog_edges),
                ),
            )
        )
        snap = self.save(
            assets,
            parent_revision=accepted_before,
            explicit_edges=list(old_edges.values()),
            state=state,
        )
        return snap

    def retain(self) -> GraphSnapshot:
        snap = self.accepted
        self.working_revision = snap.revision
        return snap

    def _content_hash(
        self,
        assets: list[NodeAsset],
        edges: list[GraphEdge],
        explicit_edges=(),
        graph: GraphSpace | None = None,
    ) -> str:
        return fingerprint(
            {
                "assets": [
                    a.model_dump(mode="json")
                    for a in sorted(assets, key=lambda a: a.id)
                ],
                "edges": [
                    e.model_dump(mode="json") for e in sorted(edges, key=lambda e: e.id)
                ],
                "explicit_edges": [
                    e.model_dump(mode="json")
                    for e in sorted(explicit_edges or [], key=lambda e: e.id)
                ],
                "schema": self.schema_version,
                "graph": graph.model_dump(mode="json") if graph is not None else None,
            }
        )


def graph_diff(before: GraphSnapshot, after: GraphSnapshot) -> list[str]:
    old = snapshot_items(before)
    new = snapshot_items(after)
    old.update({e.id: e.model_dump(mode="json") for e in before.explicit_edges})
    new.update({e.id: e.model_dump(mode="json") for e in after.explicit_edges})
    return sorted(
        key for key in old.keys() | new.keys() if old.get(key) != new.get(key)
    )


def snapshot_items(snapshot):
    """One diff/review item per editable owner, including its compact candidates."""
    items = {a.id: a.model_dump(mode="json") for a in snapshot.editable_assets}
    nodes = {n.id: n for n in snapshot.graph.nodes}
    for conflict in snapshot.conflict_nodes:
        items[conflict.id] = {
            "node": nodes[conflict.id].model_dump(mode="json"),
        }
    return items


def semantic_asset(node):
    """Rebuild the existing authoring envelope after resolving a semantic candidate."""
    from cortex.capabilities.sql_training.core.enums import SourceType
    from cortex.capabilities.sql_training.models import Source

    if node.node_type not in {kind.value for kind in SEMANTIC_TYPES}:
        raise ValueError("A formal physical node cannot be supplied as a semantic edit")
    properties = dict(node.properties)
    properties.pop("definition", None)
    properties = {key: value for key, value in properties.items() if value is not None}
    return NodeAsset(
        id=node.id,
        node_type=node.node_type,
        name=node.name,
        description=node.description,
        aliases=list(node.aliases),
        tags=list(node.tags),
        properties=properties,
        sources=[
            Source(source_type=SourceType.QA, source_id=s.case_id or s.source_id)
            for s in node.sources
        ],
    )
