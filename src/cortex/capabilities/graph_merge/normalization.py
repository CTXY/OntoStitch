from __future__ import annotations

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
    """Normalize match members while preserving positional group/expression identity."""

    def normalize(
        self, group: FinalNodeGroup, schema: SchemaDefinition
    ) -> GroupNormalizationResult:
        del schema
        members = []
        for member in group.members:
            properties = dict(member.content.properties)
            if "targets" in properties:
                # Outer order indexes target_expressions and optional edge target_id.
                # Do not reorder or union groups without remapping those identities.
                properties["targets"] = [
                    sorted(set(target)) for target in properties["targets"]
                ]
            # Keep expressions as executable text. _predicate_key produces a
            # comparison key, not a replacement serialized expression.
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
            group=group.model_copy(update={"members": tuple(members)})
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
    """A grouped target contains joint physical IDs, never an old target record."""
    return _canonical(sorted(set(target)) if isinstance(target, list) else target)



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
