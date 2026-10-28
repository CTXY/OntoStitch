from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from cortex.capabilities.graph_merge.models import FinalNodeGroup
from cortex.schema import SchemaDefinition

EdgePropertyMappingKey = tuple[str, str, str]


@dataclass(frozen=True)
class GroupNormalizationResult:
    group: FinalNodeGroup
    edge_property_mappings: dict[EdgePropertyMappingKey, Any] = field(
        default_factory=dict
    )


class NodeGroupNormalizer(Protocol):
    def normalize(
        self, group: FinalNodeGroup, schema: SchemaDefinition
    ) -> GroupNormalizationResult: ...


class NodeGroupNormalizerRegistry:
    """Resolve optional type strategies without teaching the Operator domain types."""

    def __init__(
        self,
        registrations: dict[str, tuple[frozenset[str], NodeGroupNormalizer]]
        | None = None,
    ) -> None:
        self._registrations = dict(registrations or {})

    def normalize(
        self, group: FinalNodeGroup, schema: SchemaDefinition
    ) -> GroupNormalizationResult:
        registration = self._registrations.get(group.node_type)
        if registration is None:
            return GroupNormalizationResult(group=group)
        required_fields, normalizer = registration
        if not required_fields <= set(schema.effective_node_fields(group.node_type)):
            return GroupNormalizationResult(group=group)
        return normalizer.normalize(group, schema)


def edge_property_mapping_key(
    owner_node_id: str, property_id: str, value: Any
) -> EdgePropertyMappingKey:
    return owner_node_id, property_id, _canonical(value)


class ConceptTargetNormalizer:
    """Canonicalize semantically equal Concept targets and their authored Edge references."""

    def normalize(
        self, group: FinalNodeGroup, schema: SchemaDefinition
    ) -> GroupNormalizationResult:
        del schema
        canonical_by_semantics: dict[str, str] = {}
        semantics_by_id: dict[str, str] = {}
        ordered = sorted(
            group.members,
            key=lambda item: (item.ref.origin != "existing_node", item.ref.ref_id),
        )
        reserved_ids = {
            target["id"]
            for member in ordered
            for target in member.content.properties.get("targets") or ()
            if isinstance(target, dict) and isinstance(target.get("id"), str)
        }
        for member in ordered:
            local_ids: dict[str, str] = {}
            for target in member.content.properties.get("targets") or ():
                if not isinstance(target, dict):
                    continue
                target_id = target.get("id")
                if isinstance(target_id, str) and target_id:
                    key = concept_target_semantic_key(target)
                    if target_id in local_ids and local_ids[target_id] != key:
                        raise ValueError(
                            f"ambiguous target ID in {member.ref.ref_id}: {target_id}"
                        )
                    local_ids[target_id] = key
                    if key in canonical_by_semantics:
                        continue
                    candidate = target_id
                    if candidate in semantics_by_id:
                        suffix = hashlib.sha256(key.encode()).hexdigest()[:16]
                        candidate = f"{target_id[:80]}_{suffix}"
                        while candidate in semantics_by_id or candidate in reserved_ids:
                            candidate += "_"
                    canonical_by_semantics[key] = candidate
                    semantics_by_id[candidate] = key

        mappings: dict[EdgePropertyMappingKey, Any] = {}
        members = []
        for member in group.members:
            properties = dict(member.content.properties)
            targets = []
            for value in properties.get("targets") or ():
                if not isinstance(value, dict):
                    targets.append(value)
                    continue
                target = dict(value)
                old_id = target.get("id")
                canonical_id = canonical_by_semantics.get(
                    concept_target_semantic_key(target), old_id
                )
                if isinstance(old_id, str) and isinstance(canonical_id, str):
                    mappings[
                        edge_property_mapping_key(
                            member.ref.ref_id, "target_id", old_id
                        )
                    ] = canonical_id
                    target["id"] = canonical_id
                targets.append(target)
            if "targets" in properties:
                properties["targets"] = sorted(targets, key=concept_target_semantic_key)
            members.append(
                member.model_copy(
                    update={
                        "content": member.content.model_copy(
                            update={"properties": properties}
                        )
                    }
                )
            )
        return GroupNormalizationResult(
            group=group.model_copy(update={"members": tuple(members)}),
            edge_property_mappings=mappings,
        )


def build_default_node_group_normalizer_registry() -> NodeGroupNormalizerRegistry:
    return NodeGroupNormalizerRegistry(
        {
            "concept": (
                frozenset({"targets"}),
                ConceptTargetNormalizer(),
            )
        }
    )


def concept_target_semantic_key(target: Any) -> str:
    if not isinstance(target, dict):
        return _canonical(target)
    return _canonical(
        {
            "sql_expression": _predicate_key(target.get("sql_expression")),
            "references": sorted(set(target.get("references", []))),
        }
    )


def _predicate_key(sql: Any) -> Any:
    """Only reorder pure AND/OR predicates; unsupported SQL stays byte-for-byte exact."""
    if not isinstance(sql, str):
        return sql
    try:
        expressions = sqlglot.parse(sql)
        if len(expressions) != 1 or expressions[0] is None:
            return sql
        tree = expressions[0]
        allowed = (
            exp.And,
            exp.Or,
            exp.Paren,
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.Is,
            exp.Column,
            exp.Identifier,
            exp.Literal,
            exp.Null,
            exp.Boolean,
        )
        if any(type(node) not in allowed for node in tree.walk()):
            return sql

        def key(node: exp.Expression) -> Any:
            if isinstance(node, exp.Paren):
                return key(node.this)
            if isinstance(node, (exp.And, exp.Or)):
                children: set[str] = set()

                def collect(child: exp.Expression) -> None:
                    while isinstance(child, exp.Paren):
                        child = child.this
                    if type(child) is type(node):
                        collect(child.this)
                        collect(child.expression)
                    else:
                        children.add(_canonical(key(child)))

                collect(node)
                return [node.key, sorted(children)]
            return node.sql()

        # Keep a tagged structure so an unsupported raw string cannot collide with it.
        return {"pure_predicate": key(tree)}
    except (SqlglotError, ValueError, RecursionError):
        return sql


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ConceptTargetNormalizer",
    "EdgePropertyMappingKey",
    "GroupNormalizationResult",
    "NodeGroupNormalizer",
    "NodeGroupNormalizerRegistry",
    "build_default_node_group_normalizer_registry",
    "concept_target_semantic_key",
    "edge_property_mapping_key",
]
