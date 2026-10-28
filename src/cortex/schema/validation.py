from __future__ import annotations

import hashlib
import json
from typing import Any

from cortex.schema.graph import (
    GraphEdge,
    GraphSpace,
    GraphState,
    MergeStatus,
    SourceReference,
    alternative_content,
)
from cortex.schema.models import FieldDefinition, IdentityPolicy, SchemaDefinition


class GraphSchemaError(ValueError):
    """Raised when a formal graph violates its declared Schema."""


class InvalidGraphState(GraphSchemaError):
    """Raised when a merge input state violates graph or conflict invariants."""


def calculate_schema_hash(schema: SchemaDefinition) -> str:
    """Return a deterministic content identity without storing version metadata."""

    payload = json.dumps(
        schema.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def validate_graph_space(graph: GraphSpace, schema: SchemaDefinition) -> None:
    node_types = schema.node_type_index()
    edge_types = schema.edge_type_index()
    node_by_id = {node.id: node for node in graph.nodes}

    for node in graph.nodes:
        node_type = node_types.get(node.node_type)
        if node_type is None:
            raise GraphSchemaError(
                f"node {node.id!r} uses unknown type {node.node_type!r}"
            )
        _validate_properties(
            node.properties,
            {
                key: field.model_copy(update={"required": False})
                for key, field in schema.effective_node_fields(node_type.id).items()
            }
            if node.merge_status == MergeStatus.CONFLICT
            else schema.effective_node_fields(node_type.id),
            f"node {node.id!r}",
        )

    for edge in graph.edges:
        edge_type = edge_types.get(edge.edge_type)
        if not isinstance(edge.edge_type, str) or not edge.edge_type.strip():
            raise GraphSchemaError("relation name must be nonempty")
        try:
            json.dumps(edge.properties, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise GraphSchemaError(
                "relation properties must be finite JSON values"
            ) from exc
        source = node_by_id.get(edge.from_node_id)
        target = node_by_id.get(edge.to_node_id)
        if source is None or target is None:
            raise GraphSchemaError(f"edge {edge.id!r} references an unknown endpoint")
        if edge_type is None:
            continue
        if not any(
            schema.is_node_type_compatible(source.node_type, expected)
            for expected in edge_type.from_node_types
        ):
            raise GraphSchemaError(f"edge {edge.id!r} has an invalid source node type")
        if not any(
            schema.is_node_type_compatible(target.node_type, expected)
            for expected in edge_type.to_node_types
        ):
            raise GraphSchemaError(f"edge {edge.id!r} has an invalid target node type")
        _validate_properties(
            {
                k: v
                for k, v in edge.properties.items()
                if k not in {"target_id", "description", "alternative_ids"}
                or k in edge_type.fields
            },
            edge_type.fields,
            f"edge {edge.id!r}",
        )
        if edge.edge_type == "joins_with":
            _validate_physical_join(edge, node_by_id)


def _validate_physical_join(edge: GraphEdge, nodes: dict) -> None:
    if edge.from_node_id == edge.to_node_id:
        raise GraphSchemaError("joins_with requires distinct physical table endpoints")
    pairs = edge.properties.get("column_pairs")
    if not pairs:
        raise GraphSchemaError("joins_with requires non-empty column_pairs")
    seen = set()
    for pair in pairs:
        if set(pair) != {"from_column_ref", "to_column_ref"}:
            raise GraphSchemaError("invalid joins_with column pair")
        for key, table_id in (
            ("from_column_ref", edge.from_node_id),
            ("to_column_ref", edge.to_node_id),
        ):
            reference = pair[key]
            column = nodes.get(reference) if isinstance(reference, str) else None
            if (
                column is None
                or column.node_type != "column"
                or column.properties.get("table_ref") != table_id
            ):
                raise GraphSchemaError(
                    "join column must exist and belong to its table endpoint"
                )
        identity = (pair["from_column_ref"], pair["to_column_ref"])
        if identity in seen:
            raise GraphSchemaError("duplicate joins_with column pair")
        seen.add(identity)


def validate_graph_state(state: GraphState, schema: SchemaDefinition) -> None:
    """Validate a closed, persistable graph snapshot and its conflict overlay."""

    try:
        validate_graph_space(state.graph, schema)
        _validate_conflicts(state, schema)
    except GraphSchemaError:
        raise
    except ValueError as exc:
        raise InvalidGraphState(str(exc)) from exc


def validate_incoming_graph_state(
    state: GraphState,
    schema: SchemaDefinition,
    *,
    existing: GraphSpace | None = None,
) -> None:
    """Validate an open incoming state whose endpoints may reference existing nodes."""

    if state.conflict_nodes:
        raise InvalidGraphState("incoming conflict nodes must be empty")
    overlay_nodes = (*(() if existing is None else existing.nodes), *state.graph.nodes)
    overlay = GraphSpace(nodes=overlay_nodes, edges=state.graph.edges)
    validate_graph_space(overlay, schema)


def _validate_conflicts(state: GraphState, schema: SchemaDefinition) -> None:
    nodes = {node.id: node for node in state.graph.nodes}
    conflicts = {node.id: node for node in state.conflict_nodes}
    marked = {
        node.id
        for node in state.graph.nodes
        if node.merge_status == MergeStatus.CONFLICT
    }
    if marked != set(conflicts):
        raise InvalidGraphState(
            "node merge_status and inline conflict metadata must correspond exactly"
        )
    for node_id, conflict in conflicts.items():
        conflict.as_conflict()  # Validate the complete inline metadata contract.
        node = nodes.get(node_id)
        if node is None:
            raise InvalidGraphState(f"conflict references unknown node {node_id!r}")
        node_type = schema.node_type_index()[node.node_type]
        for path in conflict.alternatives[0].overrides:
            if (
                path in {"name", "description", "aliases", "tags"}
                and getattr(node, path)
                != {"name": node.id, "description": None, "aliases": [], "tags": []}[
                    path
                ]
            ):
                raise InvalidGraphState(
                    f"variable field {path!r} must use its empty display placeholder on the common node"
                )
            if path.startswith("properties.") and path.split(".")[1] in node.properties:
                raise InvalidGraphState(
                    f"variable field {path!r} must not also appear on the common node"
                )
        expected_sources = _stable_sources(
            source
            for alternative in conflict.alternatives
            for source in alternative.sources
        )
        if node.sources != expected_sources:
            raise InvalidGraphState(
                f"conflict node {node_id!r} sources are not the alternatives union"
            )
        canonical_exact = _identity_values(
            _node_content(node), node_type.identity_policy
        )
        for alternative in conflict.alternatives:
            content = alternative_content(node, alternative).model_dump(mode="json")
            required = {"name", "description", "aliases", "tags", "properties"}
            if set(content) != required:
                raise InvalidGraphState(
                    f"conflict alternative {alternative.alternative_id!r} is not complete"
                )
            _validate_properties(
                content["properties"],
                schema.effective_node_fields(node.node_type),
                f"conflict alternative {alternative.alternative_id!r}",
            )
            if node_type.identity_policy.mode == "exact" and (
                _identity_values(content, node_type.identity_policy) != canonical_exact
            ):
                raise InvalidGraphState(
                    f"conflict alternative {alternative.alternative_id!r} changes exact identity"
                )


def _node_content(node: Any) -> dict[str, Any]:
    return {
        "name": node.name,
        "description": node.description,
        "aliases": node.aliases,
        "tags": node.tags,
        "properties": node.properties,
    }


def _identity_values(
    content: dict[str, Any], policy: IdentityPolicy
) -> tuple[Any, ...]:
    values: list[Any] = []
    for path in policy.fields:
        if path.startswith("properties."):
            values.append(content["properties"].get(path.removeprefix("properties.")))
        else:
            values.append(content.get(path))
    return tuple(values)


def _stable_sources(sources: Any) -> tuple[SourceReference, ...]:
    by_key = {
        (source.source_id, source.chunk_id, source.locator, source.case_id): source
        for source in sources
    }
    return tuple(
        by_key[key]
        for key in sorted(by_key, key=lambda item: tuple(x or "" for x in item))
    )


def _validate_properties(
    properties: dict[str, Any],
    fields: dict[str, FieldDefinition],
    label: str,
) -> None:
    unknown = set(properties) - set(fields)
    if unknown:
        raise GraphSchemaError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}"
        )
    missing = [field_id for field_id, field in fields.items() if field.required]
    missing = [field_id for field_id in missing if field_id not in properties]
    if missing:
        raise GraphSchemaError(
            f"{label} is missing required fields: {', '.join(sorted(missing))}"
        )
    for field_id, value in properties.items():
        field = fields[field_id]
        if not _matches_type(value, field.type):
            raise GraphSchemaError(
                f"{label} field {field_id!r} does not match type {field.type!r}"
            )
        if field.enum is not None and value not in field.enum:
            raise GraphSchemaError(f"{label} field {field_id!r} is outside its enum")


def _matches_type(value: Any, expression: str) -> bool:
    return any(_matches_token(value, token) for token in expression.split("|"))


def _matches_token(value: Any, token: str) -> bool:
    if token.endswith("[]"):
        item_type = token[:-2]
        return isinstance(value, list) and all(
            _matches_token(item, item_type) for item in value
        )
    if token == "string":
        return isinstance(value, str)
    if token == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if token == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if token == "boolean":
        return isinstance(value, bool)
    if token in {"date", "datetime"}:
        return isinstance(value, str)
    if token == "object":
        return isinstance(value, dict)
    if token == "array":
        return isinstance(value, list)
    return False
