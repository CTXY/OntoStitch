from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.models import (
    ColumnProperties,
    ConceptProperties,
    DimensionValueProperties,
    ExampleProperties,
    GenerationRuleProperties,
    MetricProperties,
    NodeAsset,
    TableProperties,
)
from cortex.capabilities.sql_training.models.semantic_mapping import (
    semantic_endpoint_references,
)
from cortex.schema import GraphEdge, SourceReference

RELATIONSHIP_COMPILER_VERSION = "2"


@dataclass(frozen=True)
class RelationshipCompilation:
    outgoing_edges: tuple[GraphEdge, ...]
    diagnostics: tuple[str, ...] = ()


def compile_relationships(
    asset: NodeAsset,
    *,
    endpoint_ids: Mapping[str, str] | None = None,
    endpoint_types: Mapping[str, NodeType] | None = None,
    assets: Mapping[str, NodeAsset] | None = None,
) -> RelationshipCompilation:
    """Compile one asset's declared relationships without semantic prefix inference.

    ``endpoint_ids=None`` creates an open case fragment whose targets retain authoring IDs.
    Supplying an endpoint index makes compilation fail closed by reporting and omitting dangling
    or type-incompatible declarations.
    """

    declarations = _declarations(asset, assets or {})
    diagnostics: list[str] = []
    edges: dict[tuple[str, str, str], GraphEdge] = {}
    source_id = endpoint_ids.get(asset.id, asset.id) if endpoint_ids is not None else asset.id
    for edge_type, target_ref, properties in declarations:
        target_id = target_ref
        if endpoint_ids is not None:
            resolved = endpoint_ids.get(target_ref)
            if resolved is None:
                diagnostics.append(
                    f"relationship target does not exist: {asset.id} -> {target_ref}"
                )
                continue
            target_id = resolved
        if endpoint_types is not None:
            target_type = endpoint_types.get(target_ref)
            allowed = _TARGET_TYPES[edge_type]
            if target_type is None or target_type not in allowed:
                diagnostics.append(
                    f"relationship target type is invalid: {asset.id} {edge_type} {target_ref}"
                )
                continue
        edge = _edge(asset, edge_type, source_id, target_id, target_ref, properties)
        key = (edge.edge_type, edge.to_node_id, _canonical(edge.properties))
        existing = edges.get(key)
        if existing is None:
            edges[key] = edge
        else:
            edges[key] = existing.model_copy(
                update={"sources": _merge_sources(existing.sources, edge.sources)}
            )
    if isinstance(asset.properties, GenerationRuleProperties) and diagnostics:
        raise ValueError(
            "Generation Rule dependency closure is invalid: " + "; ".join(sorted(set(diagnostics)))
        )
    return RelationshipCompilation(
        outgoing_edges=tuple(sorted(edges.values(), key=lambda item: item.id)),
        diagnostics=tuple(sorted(set(diagnostics))),
    )


def compile_all_relationships(
    assets: Iterable[NodeAsset],
    *,
    endpoint_ids: Mapping[str, str] | None = None,
) -> RelationshipCompilation:
    ordered = sorted(assets, key=lambda item: (item.node_type.value, item.id))
    asset_index = {asset.id: asset for asset in ordered}
    endpoint_types = {asset.id: asset.node_type for asset in ordered} if endpoint_ids else None
    edges: list[GraphEdge] = []
    diagnostics: list[str] = []
    for asset in ordered:
        result = compile_relationships(
            asset,
            endpoint_ids=endpoint_ids,
            endpoint_types=endpoint_types,
            assets=asset_index,
        )
        edges.extend(result.outgoing_edges)
        diagnostics.extend(result.diagnostics)
    return RelationshipCompilation(
        outgoing_edges=tuple(sorted(edges, key=lambda item: item.id)),
        diagnostics=tuple(sorted(set(diagnostics))),
    )


def _declarations(
    asset: NodeAsset,
    assets: Mapping[str, NodeAsset],
) -> list[tuple[str, str, dict[str, Any]]]:
    data = asset.properties
    result: list[tuple[str, str, dict[str, Any]]] = []
    if isinstance(data, ColumnProperties):
        table_ref = next((ref for ref in data.references if ref.startswith("table.")), None)
        table_ref = table_ref or _derived_table_ref(asset.id)
        if table_ref:
            table = assets.get(table_ref)
            table_data = (
                table.properties
                if table and isinstance(table.properties, TableProperties)
                else None
            )
            names = {asset.id, data.column_name}
            result.append(
                (
                    "belongs_to",
                    table_ref,
                    {
                        "is_default_time": bool(
                            table_data and table_data.default_time_column in names
                        ),
                        "is_important": bool(
                            table_data
                            and any(item in names for item in table_data.important_columns)
                        ),
                        "is_available_time": bool(
                            table_data
                            and any(item in names for item in table_data.available_time_columns)
                        ),
                        "is_primary_key": bool(
                            table_data and any(item in names for item in table_data.primary_keys)
                        ),
                        "is_foreign_key": bool(
                            table_data and any(item in names for item in table_data.foreign_keys)
                        ),
                    },
                )
            )
    elif isinstance(data, DimensionValueProperties):
        result.extend(
            ("value_of", ref, {})
            for ref in data.references
            if ref.startswith("column.")
        )
    elif isinstance(data, MetricProperties):
        result.extend(
            ("depends_on", ref, {})
            for ref in data.references
            if ref.startswith("metric.")
        )
        result.extend(
            ("grounded_on", ref, {})
            for ref in data.references
            if ref.startswith(("table.", "column.", "dimension_value."))
        )
    elif isinstance(data, GenerationRuleProperties):
        result.extend(
            (
                _generation_rule_edge_type(ref),
                ref,
                {},
            )
            for ref in data.references
        )
    elif isinstance(data, ConceptProperties):
        for target in data.targets:
            for endpoint in semantic_endpoint_references(target.references):
                result.append(
                    (
                        (
                            "maps_to_value"
                            if endpoint.startswith("dimension_value.")
                            else "maps_to_column"
                        ),
                        endpoint,
                        {
                            "target_id": target.id,
                            "binding_operator": target.binding_operator,
                        },
                    )
                )
    elif isinstance(data, ExampleProperties):
        result.extend(("uses", ref, {}) for ref in data.references)
    return result


def _generation_rule_edge_type(reference: str) -> str:
    if reference.startswith(("metric.", "concept.")):
        return "applies_to"
    if reference.startswith(("table.", "column.", "dimension_value.")):
        return "grounded_on"
    raise ValueError(f"unsupported Generation Rule reference target: {reference}")


_TARGET_TYPES: dict[str, frozenset[NodeType]] = {
    "belongs_to": frozenset({NodeType.TABLE}),
    "value_of": frozenset({NodeType.COLUMN}),
    "depends_on": frozenset({NodeType.METRIC}),
    "computed_from": frozenset({NodeType.COLUMN}),
    "analyzable_by": frozenset({NodeType.COLUMN}),
    "filters_on": frozenset({NodeType.COLUMN}),
    "maps_to_column": frozenset({NodeType.COLUMN}),
    "maps_to_value": frozenset({NodeType.DIMENSION_VALUE}),
    "applies_to": frozenset({NodeType.METRIC, NodeType.CONCEPT}),
    "grounded_on": frozenset({NodeType.TABLE, NodeType.COLUMN, NodeType.DIMENSION_VALUE}),
    "uses": frozenset(NodeType),
}


def _edge(
    asset: NodeAsset,
    edge_type: str,
    source_id: str,
    target_id: str,
    target_ref: str,
    properties: dict[str, Any],
) -> GraphEdge:
    identity = _canonical(
        {
            "compiler_version": RELATIONSHIP_COMPILER_VERSION,
            "source": asset.id,
            "relation": edge_type,
            "target": target_ref,
            "properties": properties,
        }
    )
    return GraphEdge(
        id=f"edge_{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
        edge_type=edge_type,
        from_node_id=source_id,
        to_node_id=target_id,
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


def _merge_sources(
    left: tuple[SourceReference, ...], right: tuple[SourceReference, ...]
) -> tuple[SourceReference, ...]:
    by_key = {
        (item.source_id, item.chunk_id, item.locator, item.case_id): item
        for item in (*left, *right)
    }
    return tuple(by_key[key] for key in sorted(by_key))


def _derived_table_ref(column_id: str) -> str | None:
    name = column_id.removeprefix("column.")
    table_name, separator, _ = name.partition(".")
    return f"table.{table_name}" if column_id.startswith("column.") and separator else None


def _filter_properties(value: dict[str, Any]) -> dict[str, Any]:
    operator = str(value.get("operator") or "=").upper().replace(" ", "_")
    if operator == "==":
        operator = "="
    raw_values = value.get("values", value.get("value", []))
    values = raw_values if isinstance(raw_values, list) else [raw_values]
    result: dict[str, Any] = {"operator": operator, "values": values}
    refs = value.get("value_refs") or ([value["value_ref"]] if value.get("value_ref") else [])
    if refs:
        result["value_refs"] = refs
    if value.get("pattern") is not None:
        result["pattern"] = value["pattern"]
    return result


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "RELATIONSHIP_COMPILER_VERSION",
    "RelationshipCompilation",
    "compile_all_relationships",
    "compile_relationships",
]
