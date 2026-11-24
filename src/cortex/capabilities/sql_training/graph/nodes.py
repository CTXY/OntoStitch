"""Create canonical nodes at generation time; authoring assets are input envelopes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from cortex.capabilities.sql_training.models import (
    ColumnProperties,
    ConceptProperties,
    DimensionValueProperties,
    EventProperties,
    GenerationRuleProperties,
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
)


def build_node(asset: NodeAsset, formal_id: str | None = None) -> GraphNode:
    """Project one authoring asset without allocating or persisting identities."""
    data = asset.properties
    type_id = asset.node_type.value
    name = asset.name or asset.id.split(".", 1)[-1]
    properties: dict[str, Any]
    if isinstance(data, TableProperties):
        properties = _compact(data.model_dump(mode="json"))
        name = asset.name or data.table_name
    elif isinstance(data, ColumnProperties):
        table_ref = _first_ref(data.references, "table.") or _derived_table_ref(
            asset.id
        )
        role = data.semantic_role.value if data.semantic_role else None
        role = "IDENTIFIER" if role == "ID" else role
        role = (
            role
            if role in {None, "DIMENSION", "MEASURE", "TIME", "IDENTIFIER"}
            else None
        )
        properties = _compact(
            {
                "table_ref": table_ref,
                "column_name": data.column_name,
                "data_type": data.data_type or "",
                "sample_values": data.sample_values or None,
                "source_metadata": data.source_metadata or None,
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
    elif isinstance(data, (MetricProperties, GenerationRuleProperties)):
        properties = _compact(data.model_dump(mode="json"))
    elif isinstance(data, ConceptProperties):
        properties = _compact(
            {
                "definition": asset.description,
                "mapping_type": data.mapping_type,
                "targets": data.targets,
                "target_expressions": data.target_expressions,
            }
        )
    elif isinstance(data, EventProperties):
        properties = {
            "definition": asset.description,
            "targets": data.targets,
                "target_expressions": data.target_expressions,
        }
    else:
        raise TypeError(f"Unsupported SQL-training asset: {asset.node_type}")
    # Targets describe matches; explicit edges are authoritative adjacency.
    # GraphSpace exposes authoritative adjacency through typed GraphEdges only.

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


def _first_ref(values: list[str], prefix: str) -> str | None:
    return next((value for value in values if value.startswith(prefix)), None)


def _derived_table_ref(column_id: str) -> str | None:
    name = column_id.removeprefix("column.")
    table_name, separator, _ = name.partition(".")
    return (
        f"table.{table_name}" if column_id.startswith("column.") and separator else None
    )


def build_graph(
    assets: Iterable[NodeAsset],
    catalog_edges: Iterable[GraphEdge],
    schema: SchemaDefinition,
    *,
    explicit_edges: Iterable[GraphEdge] = (),
) -> GraphSpace:
    from cortex.application.ontology_graph import GraphManager

    nodes = tuple(build_node(a) for a in assets)
    # The argument declares explicit intent, even when a caller copied a default edge.
    # Preserve existing overrides so removed anchors can still be detected.
    explicit_edges = tuple(
        edge.model_copy(update={"reference_origin": None})
        if edge.reference_origin == "default"
        else edge
        for edge in explicit_edges
    )
    state, _ = GraphManager(schema).prepare_state(
        GraphState(
            graph=GraphSpace(
                nodes=nodes, edges=(*explicit_edges, *tuple(catalog_edges))
            ),
        ),
    )
    return state.graph


def build_incoming_graph(
    schema: SchemaDefinition,
    *,
    assets: Iterable[NodeAsset],
    cases: Iterable[tuple[str, Iterable[NodeAsset], Iterable[GraphEdge]]],
    existing: GraphSpace | None = None,
    catalog_edges: Iterable[GraphEdge] = (),
    namespace: str = "incoming",
    relation_contracts=(),
    case_relation_contracts=None,
) -> GraphState:
    """Generate isolated case nodes and their relations at the merge boundary.

    Physical aliases come from structured owner metadata. Semantic references
    resolve only within their case, never by another case's name or position.
    """
    import hashlib
    import json

    from cortex.application.ontology_graph import GraphManager, GraphRejected, issue
    from cortex.capabilities.graph_merge.integration.contracts import isolate_contracts
    from cortex.capabilities.sql_training.core.ids import make_node_id
    from cortex.schema.references import remap_references
    from cortex.schema.relation_metadata import remap_metadata_edges

    def identity(*parts):
        return hashlib.sha256(
            json.dumps(parts, ensure_ascii=False).encode()
        ).hexdigest()[:24]

    def reject(message):
        raise GraphRejected(
            [
                issue(
                    "AMBIGUOUS_IDENTITY",
                    message,
                    "Supply unique IDs and explicit physical owners.",
                )
            ]
        )

    existing = existing if existing is not None else GraphSpace()
    relation_contracts, base_predicate_ids = isolate_contracts(existing.relation_contracts, relation_contracts,
                                             schema.edge_type_index())
    aliases = {n.id: n.id for n in existing.nodes}
    physical = {n.id: n for n in existing.nodes}

    def alias(key, value):
        if key in aliases and aliases[key] != value:
            reject(f"Ambiguous physical reference {key!r}.")
        aliases[key] = value

    for node in existing.nodes:
        if node.node_type == "table":
            alias(make_node_id("table", node.properties["table_name"]), node.id)
    for node in existing.nodes:
        if node.node_type == "column":
            table = physical.get(node.properties.get("table_ref"))
            if table:
                alias(
                    make_node_id(
                        "column",
                        table.properties["table_name"],
                        node.properties["column_name"],
                    ),
                    node.id,
                )
    for node in existing.nodes:
        if node.node_type == "dimension_value":
            column = physical.get(node.properties.get("column_ref"))
            table = physical.get(column.properties.get("table_ref")) if column else None
            if table:
                alias(
                    make_node_id(
                        "dimension_value",
                        table.properties["table_name"],
                        column.properties["column_name"],
                        node.properties["value"],
                    ),
                    node.id,
                )

    catalog_edges = tuple(catalog_edges)
    base_edge_ids = {e.id: f"incoming_edge_{identity(namespace, e.id)}" for e in catalog_edges}
    values = list(assets)
    if len({a.id for a in values}) != len(values):
        reject("Duplicate generated asset IDs.")
    base_ids = {
        a.id: (
            f"incoming_node_{identity(namespace, a.id)}" if a.id in physical else a.id
        )
        for a in values
    }
    refs = {**aliases, **base_ids}
    nodes = [build_node(a, base_ids[a.id]) for a in values]
    nodes = [
        n.model_copy(update={"properties": remap_metadata_edges(remap_references(n.properties, refs), base_edge_ids)})
        for n in nodes
    ]
    case_edges = []
    occupied = set(physical) | set(base_ids.values())
    for case_id, case_assets, authored_edges in cases:
        case_assets = list(case_assets)
        authored_edges = tuple(authored_edges)
        relation_contracts, predicate_ids = isolate_contracts(
            relation_contracts, (case_relation_contracts or {}).get(case_id, ()), schema.edge_type_index())
        edge_ids = {**base_edge_ids, **{e.id: f"incoming_edge_{identity(namespace, case_id, e.id)}" for e in authored_edges}}
        local = {
            a.id: f"incoming_node_{identity(namespace, case_id, a.id)}"
            for a in case_assets
        }
        if len(local) != len(case_assets):
            reject(f"Duplicate asset IDs in case {case_id!r}.")
        if set(local.values()) & occupied:
            reject(f"Repeated case or incoming ID collision for {case_id!r}.")
        occupied.update(local.values())
        case_refs = {**refs, **local}
        for asset in case_assets:
            node = build_node(asset, local[asset.id])
            nodes.append(
                node.model_copy(
                    update={"properties": remap_metadata_edges(remap_references(node.properties, case_refs), edge_ids)}
                )
            )
        case_edges.extend(
            e.model_copy(
                update={
                    "id": edge_ids[e.id],
                    "edge_type": predicate_ids.get(e.edge_type, base_predicate_ids.get(e.edge_type, e.edge_type)),
                    "from_node_id": case_refs.get(e.from_node_id, e.from_node_id),
                    "to_node_id": case_refs.get(e.to_node_id, e.to_node_id),
                    "properties": remap_references(e.properties, case_refs),
                }
            )
            for e in authored_edges
        )
    edges = (
        *case_edges,
        *tuple(
            e.model_copy(
                update={
                    "id": base_edge_ids[e.id],
                    "edge_type": base_predicate_ids.get(e.edge_type, e.edge_type),
                    "from_node_id": refs.get(e.from_node_id, e.from_node_id),
                    "to_node_id": refs.get(e.to_node_id, e.to_node_id),
                    "properties": remap_references(e.properties, refs),
                }
            )
            for e in catalog_edges
        ),
    )
    return GraphManager(schema).prepare_fragment(
        GraphState(
            graph=GraphSpace(nodes=tuple(nodes), edges=edges, relation_contracts=tuple(relation_contracts))
        ),
        existing=existing,
    )
