from __future__ import annotations

import json
from collections.abc import Callable

from cortex.capabilities.graph_merge.models import NodeContent, NodeRepresentation
from cortex.capabilities.graph_merge.normalization import concept_target_semantic_key
from cortex.schema import NodeType, SchemaDefinition


class FuseError(ValueError):
    pass


def fuse_representations(
    representations: tuple[NodeRepresentation, ...],
    *,
    baseline: NodeRepresentation,
) -> NodeContent:
    if not representations:
        raise FuseError("cannot fuse an empty representation group")
    fuser = _FUSERS.get(baseline.node_type, _fuse_strict)
    return fuser(representations, baseline.content)


class NodeFuserRegistry:
    """Resolve Schema-compatible specialized fusers with a strict generic fallback."""

    def __init__(
        self,
        registrations: dict[
            str,
            tuple[
                frozenset[str],
                Callable[[tuple[NodeRepresentation, ...], NodeContent], NodeContent],
            ],
        ]
        | None = None,
    ) -> None:
        self._registrations = dict(registrations or {})

    def fuse(
        self,
        representations: tuple[NodeRepresentation, ...],
        *,
        baseline: NodeRepresentation,
        node_type: NodeType,
        schema: SchemaDefinition,
    ) -> NodeContent:
        registration = self._registrations.get(node_type.id)
        if registration is None:
            return _fuse_strict(representations, baseline.content)
        required_fields, fuser = registration
        if not required_fields <= set(schema.effective_node_fields(node_type.id)):
            return _fuse_strict(representations, baseline.content)
        return fuser(representations, baseline.content)


def build_default_node_fuser_registry() -> NodeFuserRegistry:
    return NodeFuserRegistry(
        {
            "concept": (frozenset({"targets"}), _fuse_concept),
            "metric": (frozenset({"expression"}), _fuse_metric),
            "generation_rule": (frozenset({"triggers"}), _fuse_generation_rule),
        }
    )


def _fuse_concept(
    representations: tuple[NodeRepresentation, ...], baseline: NodeContent
) -> NodeContent:
    properties = dict(baseline.properties)
    targets: dict[str, object] = {}
    for representation in representations:
        unexpected = set(representation.content.properties) - {"targets", "mapping_type"}
        if unexpected:
            _require_equal_properties(representations, unexpected)
        for target in representation.content.properties.get("targets", []):
            key = _concept_target_key(target)
            old = targets.get(key)
            if old is not None and _canonical(old) != _canonical(target):
                # The semantic key proves only the supported predicate reorderings.
                # Preserve one original SQL spelling, never synthesize new binding SQL.
                comparable = dict(target) if isinstance(target, dict) else {}
                if isinstance(old, dict) and not old.get("semantic_targets"):
                    comparable["sql_expression"] = old.get("sql_expression")
                if _canonical(old) != _canonical(comparable):
                    raise FuseError(f"conflicting Concept target: {key}")
                continue
            targets[key] = target
    properties["targets"] = [targets[key] for key in sorted(targets)]
    mapping_types = {
        item.content.properties.get("mapping_type")
        for item in representations
        if item.content.properties.get("mapping_type") is not None
    }
    if len(mapping_types) > 1:
        raise FuseError("conflicting Concept mapping_type")
    if mapping_types:
        properties["mapping_type"] = next(iter(mapping_types))
    return _surface_fuse(representations, baseline, properties)


def _fuse_metric(
    representations: tuple[NodeRepresentation, ...], baseline: NodeContent
) -> NodeContent:
    # A Metric's executable value contract is its expression.  Description, display name,
    # pattern, and source-local plans explain or implement that contract; prose differences do
    # not create a second formula.  Other structured fields can change the value and therefore
    # must still agree.
    # References are a set-like closure, but plans are coherent behavioral programs.  Distinct
    # source plans require semantic reconciliation so query-instance differences can be removed,
    # equivalent paths can be parameterized in prose, and incompatible behavior can remain a
    # conflict.  Blindly unioning plans bypasses all of those checks.
    union_fields = {"references"}
    semantic_fields = {"sql_generation_plans"}
    explanatory_fields = {"metric_name", "pattern"}
    core_fields = (
        set().union(*(item.content.properties for item in representations))
        - union_fields
        - semantic_fields
        - explanatory_fields
    )
    _require_equal_properties(representations, core_fields, empty_equals_missing=True)
    plan_values = {
        _canonical(item.content.properties.get("sql_generation_plans", []))
        for item in representations
    }
    if len(plan_values) > 1:
        raise FuseError("distinct sql_generation_plans require semantic reconciliation")
    properties = dict(baseline.properties)
    for field in union_fields:
        values = {
            _canonical(value): value
            for item in representations
            for value in item.content.properties.get(field, [])
        }
        if values or field in properties:
            properties[field] = [values[key] for key in sorted(values)]
    if "sql_generation_plans" in baseline.properties:
        properties["sql_generation_plans"] = baseline.properties["sql_generation_plans"]
    return _surface_fuse(representations, baseline, properties)


def _fuse_generation_rule(
    representations: tuple[NodeRepresentation, ...], baseline: NodeContent
) -> NodeContent:
    _require_equal_descriptions(representations)
    union_fields = {"triggers", "references", "postconditions"}
    core_fields = set().union(*(item.content.properties for item in representations)) - union_fields
    _require_equal_properties(representations, core_fields)
    properties = dict(baseline.properties)
    properties["references"] = _union_list(representations, "references")
    postconditions = _union_list(representations, "postconditions")
    if postconditions or "postconditions" in properties:
        properties["postconditions"] = postconditions
    properties["triggers"] = _merge_triggers(representations)
    return _surface_fuse(representations, baseline, properties)


def _fuse_strict(
    representations: tuple[NodeRepresentation, ...], baseline: NodeContent
) -> NodeContent:
    _require_equal_descriptions(representations)
    _require_equal_properties(
        representations,
        set().union(*(item.content.properties for item in representations)),
    )
    return _surface_fuse(representations, baseline, dict(baseline.properties))


def _require_equal_descriptions(
    representations: tuple[NodeRepresentation, ...],
) -> None:
    if len({item.content.description for item in representations}) > 1:
        raise FuseError("conflicting description requires semantic reconciliation")


def _surface_fuse(
    representations: tuple[NodeRepresentation, ...],
    baseline: NodeContent,
    properties: dict,
) -> NodeContent:
    aliases = {
        value
        for item in representations
        for value in (*item.content.aliases, item.content.name)
        if value and value != baseline.name
    }
    tags = {value for item in representations for value in item.content.tags}
    return baseline.model_copy(
        update={
            "aliases": tuple(sorted(aliases)),
            "tags": tuple(sorted(tags)),
            "properties": properties,
        }
    )


def _require_equal_properties(
    representations: tuple[NodeRepresentation, ...],
    fields: set[str],
    *,
    empty_equals_missing: bool = False,
) -> None:
    for field in fields:
        values = {
            _canonical(
                _normalize_optional(item.content.properties.get(field))
                if empty_equals_missing
                else item.content.properties.get(field)
            )
            for item in representations
        }
        if len(values) > 1:
            raise FuseError(f"conflicting definition field: {field}")


def _normalize_optional(value: object) -> object:
    return None if value in (None, [], {}) else value


def _concept_target_key(target: object) -> str:
    return concept_target_semantic_key(target)


def _union_list(representations: tuple[NodeRepresentation, ...], field: str) -> list:
    values = {
        _canonical(value): value
        for item in representations
        for value in item.content.properties.get(field, [])
    }
    return [values[key] for key in sorted(values)]


def _merge_triggers(representations: tuple[NodeRepresentation, ...]) -> dict:
    merged: dict[str, dict[str, list]] = {}
    for item in representations:
        for scope, fields in item.content.properties.get("triggers", {}).items():
            target = merged.setdefault(scope, {})
            for field, values in fields.items():
                current = {_canonical(value): value for value in target.get(field, [])}
                current.update({_canonical(value): value for value in values})
                target[field] = [current[key] for key in sorted(current)]
    return merged


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_FUSERS: dict[
    str, Callable[[tuple[NodeRepresentation, ...], NodeContent], NodeContent]
] = {
    "concept": _fuse_concept,
    "metric": _fuse_metric,
    "generation_rule": _fuse_generation_rule,
}


__all__ = [
    "FuseError",
    "NodeFuserRegistry",
    "build_default_node_fuser_registry",
    "fuse_representations",
]
