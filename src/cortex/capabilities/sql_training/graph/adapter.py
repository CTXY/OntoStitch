from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from cortex.capabilities.sql_training.artifacts import Workspace
from cortex.capabilities.sql_training.artifacts.jsonl import (
    read_json,
    write_json_atomic,
)
from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.graph.relationships import compile_relationships
from cortex.capabilities.sql_training.models import (
    ColumnProperties,
    ConceptProperties,
    DimensionValueProperties,
    ExampleProperties,
    GenerationRuleProperties,
    JoinPathProperties,
    MetricProperties,
    NodeAsset,
    TableProperties,
)
from cortex.schema import (
    GraphEdge,
    GraphNode,
    GraphSpace,
    GraphState,
    SchemaDefinition,
    SourceReference,
    validate_graph_space,
)

_PHYSICAL_TYPES = {NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE}


def _without_references(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_references(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _without_references(item)
        for key, item in value.items()
        if key != "references"
    }


class NodeGraphAdapter:
    """Project persistent SQL-training assets into the current GraphSpace contract."""

    def __init__(self, workspace: Workspace):
        self.workspace = workspace
        self._identity_path = workspace.root / "identity_map.json"
        self._manifest_path = workspace.root / "graph_projection.json"
        self._identities: dict[str, str] = dict(read_json(self._identity_path, default={}) or {})

    def to_graph(
        self,
        schema: SchemaDefinition,
        existing_graph: GraphSpace | None = None,
        *,
        committed_assets: Iterable[NodeAsset] | None = None,
        catalog_edges: Iterable[GraphEdge] | None = None,
    ) -> tuple[GraphSpace, tuple[str, ...]]:
        assets = list(
            committed_assets
            if committed_assets is not None
            else self.workspace.read_committed_assets()
        )
        asset_by_id = {asset.id: asset for asset in assets}
        formal_ids = self._allocate_node_ids(assets)
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = list(
            catalog_edges if catalog_edges is not None else self.workspace.read_catalog_edges()
        )
        warnings: list[str] = []

        for asset in assets:
            if asset.node_type == NodeType.JOIN_PATH:
                edge = self._legacy_join_edge(asset, formal_ids, warnings)
                if edge is not None:
                    edges.append(edge)
                continue
            node = self.project_asset_node(asset, formal_ids[asset.id])
            nodes.append(node)
            edges.extend(self._asset_edges(asset, formal_ids, asset_by_id, warnings))

        graph = self._merge_projection(existing_graph, nodes, edges, warnings)
        validate_graph_space(graph, schema)
        write_json_atomic(self._identity_path, self._identities)
        write_json_atomic(
            self._manifest_path,
            {
                "node_ids": sorted(node.id for node in nodes),
                "edge_ids": sorted(edge.id for edge in edges),
            },
        )
        return graph, tuple(warnings)

    def to_incoming_state(
        self,
        schema: SchemaDefinition,
        *,
        committed_assets: Iterable[NodeAsset],
        existing_node_ids: Iterable[str] = (),
        existing_nodes: Iterable[GraphNode] = (),
        existing_edge_ids: Iterable[str] = (),
        authoring_namespace: str = "incoming",
        catalog_edges: Iterable[GraphEdge] | None = None,
    ) -> tuple[GraphState, tuple[str, ...]]:
        """Purely project this run's assets without reading or publishing graph runtime state."""

        assets = list(committed_assets)
        existing_node_values = tuple(existing_nodes)
        existing_ids = set(existing_node_ids) | {node.id for node in existing_node_values}
        asset_by_id = {asset.id: asset for asset in assets}
        formal_ids = self._allocate_node_ids(assets)
        for asset_id, formal_id in tuple(formal_ids.items()):
            if formal_id in existing_ids:
                suffix = hashlib.sha256(f"{authoring_namespace}:{asset_id}".encode()).hexdigest()[
                    :24
                ]
                formal_ids[asset_id] = f"incoming_node_{suffix}"
        existing_endpoints = {
            **{node_id: node_id for node_id in existing_ids},
            **_existing_authoring_endpoints(existing_node_values),
        }
        existing_endpoint_types = {
            **{
                node.id: NodeType(node.node_type)
                for node in existing_node_values
                if node.node_type in NodeType._value2member_map_
            },
            **{
                authoring_id: NodeType(node.node_type)
                for node in existing_node_values
                for authoring_id, canonical_id in existing_endpoints.items()
                if canonical_id == node.id and node.node_type in NodeType._value2member_map_
            },
        }
        endpoint_ids = {**existing_endpoints, **formal_ids}
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        warnings: list[str] = []
        for asset in assets:
            if asset.node_type == NodeType.JOIN_PATH:
                edge = self._legacy_join_edge(asset, endpoint_ids, warnings)
                if edge is not None:
                    edges.append(edge)
                continue
            nodes.append(self.project_asset_node(asset, formal_ids[asset.id]))
            edges.extend(
                self._asset_edges(
                    asset,
                    endpoint_ids,
                    asset_by_id,
                    warnings,
                    existing_endpoint_types=existing_endpoint_types,
                )
            )
        edges.extend(
            catalog_edges if catalog_edges is not None else self.workspace.read_catalog_edges()
        )
        occupied_edges = set(existing_edge_ids)
        edges = [
            edge.model_copy(update={"id": _incoming_edge_id(authoring_namespace, edge.id)})
            if edge.id in occupied_edges
            else edge
            for edge in edges
        ]
        state = GraphState(
            graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges)),
            node_conflicts=(),
        )
        # Incoming may intentionally have Edge endpoints in the existing graph overlay.
        overlay_nodes = tuple(nodes)
        local_edges = tuple(
            edge
            for edge in edges
            if edge.from_node_id in {item.id for item in overlay_nodes}
            and edge.to_node_id in {item.id for item in overlay_nodes}
        )
        validate_graph_space(GraphSpace(nodes=overlay_nodes, edges=local_edges), schema)
        return state, tuple(warnings)

    def to_trace_incoming_state(
        self,
        schema: SchemaDefinition,
        *,
        committed_assets: Iterable[NodeAsset],
        case_fragments: Iterable[
            tuple[str, Iterable[NodeAsset], Iterable[GraphEdge]]
        ],
        existing_edge_ids: Iterable[str] = (),
        existing_nodes: Iterable[GraphNode] = (),
        authoring_namespace: str = "incoming",
    ) -> tuple[GraphState, tuple[str, ...]]:
        """Project persistent assets and isolated Trace case outputs into one incoming state.

        Case-local semantic IDs are authoring identities and may repeat across cases. Each
        occurrence therefore receives a deterministic transport ID. The Graph Merge operator can
        then resolve identity and reconcile content without losing colliding case evidence first.
        """

        existing_node_values = tuple(existing_nodes)
        existing_node_ids_set = {node.id for node in existing_node_values}
        existing_edges = set(existing_edge_ids)
        base, base_warnings = self.to_incoming_state(
            schema,
            committed_assets=committed_assets,
            existing_node_ids=existing_node_ids_set,
            existing_nodes=existing_node_values,
            existing_edge_ids=existing_edges,
            authoring_namespace=authoring_namespace,
        )
        fragments = [
            (case_id, list(assets), list(edges))
            for case_id, assets, edges in case_fragments
        ]
        nodes = list(base.graph.nodes)
        edges = list(base.graph.edges)
        warnings = list(base_warnings)
        occupied_node_ids = existing_node_ids_set | {node.id for node in nodes}
        occupied_edge_ids = existing_edges | {edge.id for edge in edges}
        mappings: list[tuple[str, dict[str, str], list[GraphEdge]]] = []
        first_transport_id = _existing_authoring_endpoints(existing_node_values)

        for case_id, assets, case_edges in fragments:
            asset_by_id = {asset.id: asset for asset in assets}
            if len(asset_by_id) != len(assets):
                raise ValueError(f"Trace case {case_id!r} contains duplicate asset IDs")
            local_mapping: dict[str, str] = {}
            for asset in assets:
                if asset.node_type == NodeType.JOIN_PATH:
                    raise ValueError("Trace semantic fragments cannot contain join_path assets")
                transport_id = _trace_node_id(authoring_namespace, case_id, asset.id)
                if transport_id in occupied_node_ids:
                    raise ValueError(f"Trace node transport ID collision for case {case_id!r}")
                local_mapping[asset.id] = transport_id
                first_transport_id.setdefault(asset.id, transport_id)
                occupied_node_ids.add(transport_id)
                nodes.append(self.project_asset_node(asset, transport_id))
            mappings.append((case_id, local_mapping, case_edges))

        for case_id, local_mapping, case_edges in mappings:
            for edge in case_edges:
                source_id = local_mapping.get(edge.from_node_id)
                if source_id is None:
                    raise ValueError(
                        f"Trace case {case_id!r} edge source is not owned by the case: "
                        f"{edge.from_node_id}"
                    )
                target_id = local_mapping.get(
                    edge.to_node_id,
                    first_transport_id.get(edge.to_node_id, edge.to_node_id),
                )
                transport_id = _trace_edge_id(authoring_namespace, case_id, edge.id)
                if transport_id in occupied_edge_ids:
                    raise ValueError(f"Trace edge transport ID collision for case {case_id!r}")
                occupied_edge_ids.add(transport_id)
                edges.append(
                    edge.model_copy(
                        update={
                            "id": transport_id,
                            "from_node_id": source_id,
                            "to_node_id": target_id,
                        }
                    )
                )

        state = GraphState(
            graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges)),
            node_conflicts=(),
        )
        return state, tuple(warnings)

    def to_authoring_graph(
        self,
        schema: SchemaDefinition,
        *,
        assets: Iterable[NodeAsset],
        catalog_edges: Iterable[GraphEdge] | None = None,
    ) -> tuple[GraphSpace, tuple[str, ...]]:
        """Project an ephemeral graph while preserving SQL-training authoring IDs."""

        values = list(assets)
        asset_by_id = {asset.id: asset for asset in values}
        if len(asset_by_id) != len(values):
            raise ValueError("authoring graph assets must have unique IDs")
        endpoint_ids = {
            asset.id: asset.id for asset in values if asset.node_type != NodeType.JOIN_PATH
        }
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        warnings: list[str] = []
        for asset in values:
            if asset.node_type == NodeType.JOIN_PATH:
                edge = self._legacy_join_edge(asset, endpoint_ids, warnings)
                if edge is not None:
                    edges.append(edge)
                continue
            nodes.append(self.project_asset_node(asset, asset.id))
            edges.extend(self._asset_edges(asset, endpoint_ids, asset_by_id, warnings))
        edges.extend(
            catalog_edges if catalog_edges is not None else self.workspace.read_catalog_edges()
        )
        graph = GraphSpace(nodes=tuple(nodes), edges=tuple(edges))
        validate_graph_space(graph, schema)
        return graph, tuple(warnings)

    def _allocate_node_ids(self, assets: list[NodeAsset]) -> dict[str, str]:
        result: dict[str, str] = {}
        for asset in assets:
            if asset.node_type == NodeType.JOIN_PATH:
                continue
            preferred = asset.id if asset.node_type in _PHYSICAL_TYPES else None
            identity_key = f"node:{asset.node_type.value}:{asset.id}"
            if asset.node_type == NodeType.CONCEPT and identity_key not in self._identities:
                suffix = asset.id.removeprefix("concept.")
                legacy_key = f"node:semantic_mapping:semantic_mapping.{suffix}"
                legacy_value = self._identities.pop(legacy_key, None)
                if legacy_value is not None:
                    self._identities[identity_key] = legacy_value
            result[asset.id] = self._identity(
                identity_key,
                prefix="node",
                preferred=preferred,
            )
        return result

    @staticmethod
    def project_asset_node(asset: NodeAsset, formal_id: str | None = None) -> GraphNode:
        """Project one authoring asset without allocating or persisting identities."""
        data = asset.properties
        type_id = asset.node_type.value
        name = asset.name or asset.id.split(".", 1)[-1]
        properties: dict[str, Any]
        if isinstance(data, TableProperties):
            properties = _compact(data.model_dump(mode="json"))
            name = asset.name or data.table_name
        elif isinstance(data, ColumnProperties):
            table_ref = _first_ref(data.references, "table.") or _derived_table_ref(asset.id)
            role = data.semantic_role.value if data.semantic_role else None
            role = "IDENTIFIER" if role == "ID" else role
            role = role if role in {None, "DIMENSION", "MEASURE", "TIME", "IDENTIFIER"} else None
            properties = _compact(
                {
                    "table_ref": table_ref,
                    "column_name": data.column_name,
                    "data_type": data.data_type or "",
                    "sample_values": data.sample_values or None,
                    "unit": data.unit.value if data.unit else None,
                    "nullable": data.nullable,
                    "semantic_role": role,
                    "key_role": data.key_role.value if data.key_role else None,
                    "statistics": (
                        data.statistics.model_dump(mode="json") if data.statistics else None
                    ),
                }
            )
            name = asset.name or data.column_name
        elif isinstance(data, DimensionValueProperties):
            properties = {
                "column_ref": _first_ref(data.references, "column."),
                "value": data.value,
            }
            name = asset.name or str(data.value)
        elif isinstance(data, MetricProperties):
            properties = _compact(data.model_dump(mode="json"))
        elif isinstance(data, GenerationRuleProperties):
            properties = _compact(data.model_dump(mode="json"))
        elif isinstance(data, ExampleProperties):
            properties = _compact({"task": data.task, "sql": data.sql})
            name = asset.name or data.task[:80]
        elif isinstance(data, ConceptProperties):
            properties = _compact(
                {
                    "definition": asset.description,
                    "mapping_type": data.mapping_type,
                    "targets": [target.model_dump(mode="json") for target in data.targets],
                }
            )
        else:
            raise TypeError(f"Unsupported SQL-training asset: {asset.node_type}")
        # NodeAsset references are authoring inputs consumed by the relationship compiler.
        # GraphSpace exposes authoritative adjacency through typed GraphEdges only.
        properties = _without_references(properties)
        return GraphNode(
            id=formal_id or asset.id,
            node_type=type_id,
            name=name,
            description=asset.description,
            aliases=list(asset.aliases),
            tags=list(asset.tags),
            properties=properties,
            sources=_sources(asset),
        )

    def _asset_edges(
        self,
        asset: NodeAsset,
        formal_ids: dict[str, str],
        asset_by_id: dict[str, NodeAsset],
        warnings: list[str],
        *,
        existing_endpoint_types: dict[str, NodeType] | None = None,
    ) -> list[GraphEdge]:
        endpoint_types = dict(existing_endpoint_types or {})
        endpoint_types.update({item.id: item.node_type for item in asset_by_id.values()})
        compiled = compile_relationships(
            asset,
            endpoint_ids=formal_ids,
            endpoint_types=endpoint_types,
            assets=asset_by_id,
        )
        warnings.extend(compiled.diagnostics)
        return list(compiled.outgoing_edges)

    def _legacy_join_edge(
        self,
        asset: NodeAsset,
        formal_ids: dict[str, str],
        warnings: list[str],
    ) -> GraphEdge | None:
        """Read old join_path node artifacts as edges during workspace migration."""

        data = asset.properties
        if not isinstance(data, JoinPathProperties):
            raise TypeError("legacy join_path asset must use JoinPathProperties")
        if data.from_table_ref not in formal_ids or data.to_table_ref not in formal_ids:
            warnings.append(f"SQL Training projection skipped invalid legacy join: {asset.id}")
            return None
        join_type = data.default_join_type.value if data.default_join_type else "LEFT"
        properties: dict[str, Any] = {
            "default_join_type": "FULL" if join_type == "FULL_OUTER" else join_type,
            "conditions": [item.model_dump(mode="json") for item in data.join_conditions],
        }
        if data.cardinality is not None:
            properties["cardinality"] = data.cardinality.value
        return self._edge(
            "joins_with",
            asset.id,
            data.to_table_ref,
            formal_ids[data.from_table_ref],
            formal_ids[data.to_table_ref],
            properties,
            asset,
        )

    def _edge(
        self,
        edge_type: str,
        legacy_source: str,
        legacy_target: str,
        source_id: str,
        target_id: str,
        properties: dict[str, Any],
        asset: NodeAsset,
    ) -> GraphEdge:
        key = f"node:edge:{edge_type}:{legacy_source}:{legacy_target}"
        if key not in self._identities:
            legacy_prefix = f"{key}:"
            legacy_matches = sorted(
                identity for identity in self._identities if identity.startswith(legacy_prefix)
            )
            if len(legacy_matches) == 1:
                self._identities[key] = self._identities.pop(legacy_matches[0])
        return GraphEdge(
            id=self._identity(key, prefix="edge"),
            edge_type=edge_type,
            from_node_id=source_id,
            to_node_id=target_id,
            properties=properties,
            sources=_sources(asset),
        )

    def _identity(self, key: str, *, prefix: str, preferred: str | None = None) -> str:
        existing = self._identities.get(key)
        if existing:
            return existing
        value = preferred or f"{prefix}_{hashlib.sha256(key.encode()).hexdigest()[:24]}"
        self._identities[key] = value
        return value

    def _merge_projection(
        self,
        existing_graph: GraphSpace | None,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        warnings: list[str],
    ) -> GraphSpace:
        node_by_id = {node.id: node for node in (existing_graph.nodes if existing_graph else ())}
        edge_by_id = {edge.id: edge for edge in (existing_graph.edges if existing_graph else ())}
        previous_manifest = read_json(self._manifest_path, default={}) or {}
        for edge_id in previous_manifest.get("edge_ids", []):
            edge_by_id.pop(str(edge_id), None)
        for node in nodes:
            existing = node_by_id.get(node.id)
            node_by_id[node.id] = _upsert_node(existing, node) if existing else node
        for edge in edges:
            existing = edge_by_id.get(edge.id)
            edge_by_id[edge.id] = _upsert_edge(existing, edge) if existing else edge
        valid_node_ids = set(node_by_id)
        orphaned = [
            edge_id
            for edge_id, edge in edge_by_id.items()
            if edge.from_node_id not in valid_node_ids or edge.to_node_id not in valid_node_ids
        ]
        for edge_id in orphaned:
            edge_by_id.pop(edge_id)
            warnings.append(f"SQL Training projection removed orphan edge: {edge_id}")
        return GraphSpace(
            nodes=tuple(node_by_id.values()),
            edges=tuple(edge_by_id.values()),
        )


def _upsert_node(existing: GraphNode, incoming: GraphNode) -> GraphNode:
    if existing.node_type != incoming.node_type:
        raise ValueError(
            f"Graph node ID {incoming.id!r} changes type from "
            f"{existing.node_type!r} to {incoming.node_type!r}"
        )
    return incoming.model_copy(
        update={
            "aliases": _stable_union(existing.aliases, incoming.aliases),
            "tags": _stable_union(existing.tags, incoming.tags),
            "sources": _merge_sources(existing.sources, incoming.sources),
        }
    )


def _upsert_edge(existing: GraphEdge, incoming: GraphEdge) -> GraphEdge:
    existing_identity = (
        existing.edge_type,
        existing.from_node_id,
        existing.to_node_id,
    )
    incoming_identity = (
        incoming.edge_type,
        incoming.from_node_id,
        incoming.to_node_id,
    )
    if existing_identity != incoming_identity:
        raise ValueError(
            f"Graph edge ID {incoming.id!r} changes identity from "
            f"{existing_identity!r} to {incoming_identity!r}"
        )
    return incoming.model_copy(
        update={"sources": _merge_sources(existing.sources, incoming.sources)}
    )


def _stable_union(existing: list[str], incoming: list[str]) -> list[str]:
    return list(dict.fromkeys([*existing, *incoming]))


def _merge_sources(
    existing: tuple[SourceReference, ...],
    incoming: tuple[SourceReference, ...],
) -> tuple[SourceReference, ...]:
    by_key = {
        (source.source_id, source.locator, source.chunk_id): source
        for source in (*existing, *incoming)
    }
    return tuple(by_key.values())


def _sources(asset: NodeAsset) -> tuple[SourceReference, ...]:
    return tuple(
        SourceReference(
            source_id=source.source_id or asset.id,
            locator=source.source_type.value,
            case_id=source.source_id,
        )
        for source in asset.sources
    ) or (SourceReference(source_id=f"node:{asset.id}", locator="inferred"),)


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _incoming_edge_id(namespace: str, edge_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{edge_id}".encode()).hexdigest()[:24]
    return f"incoming_edge_{digest}"


def _trace_node_id(namespace: str, case_id: str, asset_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{case_id}:{asset_id}".encode()).hexdigest()[:24]
    return f"incoming_node_{digest}"


def _trace_edge_id(namespace: str, case_id: str, edge_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{case_id}:{edge_id}".encode()).hexdigest()[:24]
    return f"incoming_edge_{digest}"


def _existing_authoring_endpoints(nodes: Iterable[GraphNode]) -> dict[str, str]:
    """Recover SQL-training authoring refs after Graph Merge assigns canonical node IDs."""

    endpoints: dict[str, str] = {}
    for node in nodes:
        if node.node_type == "table":
            table_name = node.properties.get("table_name")
            if isinstance(table_name, str):
                endpoints[f"table.{table_name}"] = node.id
        elif node.node_type == "column":
            database_source = next(
                (
                    source.source_id
                    for source in node.sources
                    if source.locator == "database" and "." in source.source_id
                ),
                None,
            )
            if database_source is not None:
                endpoints[f"column.{database_source}"] = node.id
        elif node.node_type == "dimension_value":
            column_ref = node.properties.get("column_ref")
            value = node.properties.get("value")
            if isinstance(column_ref, str) and value is not None:
                endpoints[f"dimension_value.{column_ref.removeprefix('column.')}.{value}"] = node.id
        elif node.node_type in {"concept", "metric", "generation_rule"}:
            endpoints[f"{node.node_type}.{node.name}"] = node.id
    return endpoints


def _refs(values: list[str], prefix: str) -> list[str]:
    return list(dict.fromkeys(value for value in values if value.startswith(prefix)))


def _first_ref(values: list[str], prefix: str) -> str | None:
    return next((value for value in values if value.startswith(prefix)), None)


def _derived_table_ref(column_id: str) -> str | None:
    name = column_id.removeprefix("column.")
    table_name, separator, _ = name.partition(".")
    return f"table.{table_name}" if column_id.startswith("column.") and separator else None


def _filter_ref(value: dict[str, Any]) -> str | None:
    ref = value.get("column_ref")
    return str(ref) if ref else None


def _filter_properties(value: dict[str, Any]) -> dict[str, Any]:
    operator = str(value.get("operator") or "=").upper().replace(" ", "_")
    if operator == "==":
        operator = "="
    raw_values = value.get("values", value.get("value", []))
    values = raw_values if isinstance(raw_values, list) else [raw_values]
    result: dict[str, Any] = {"operator": operator, "values": values}
    value_ref = value.get("value_ref")
    value_refs = value.get("value_refs")
    refs = value_refs if isinstance(value_refs, list) else [value_ref] if value_ref else []
    if refs:
        result["value_refs"] = refs
    if value.get("pattern") is not None:
        result["pattern"] = value["pattern"]
    return result


__all__ = ["NodeGraphAdapter"]
