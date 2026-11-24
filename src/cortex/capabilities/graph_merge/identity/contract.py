"""Identity decisions and exact physical keys; no graph mutation or LLM calls."""
from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from cortex.capabilities.graph_merge.models import Model
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL
from cortex.schema import GraphNode, IdentityPolicy, NodeContent

class IdentityGroup(Model):
    members: tuple[str, ...] = Field(min_length=2)
    meaning: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Distinction(Model):
    members: tuple[str, str]
    reason: str = Field(min_length=1)


class IdentityDecision(Model):
    groups: tuple[IdentityGroup, ...] = ()
    distinctions: tuple[Distinction, ...] = ()


def node_content(node: GraphNode) -> NodeContent:
    return NodeContent(
        name=node.name,
        description=node.description,
        aliases=tuple(node.aliases),
        tags=tuple(node.tags),
        properties=node.properties,
    )


def identity_values(content: NodeContent, policy: IdentityPolicy) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for path in policy.fields:
        if path.startswith("properties."):
            values[path] = content.properties.get(path.removeprefix("properties."))
        else:
            values[path] = getattr(content, path)
    return values


def exact_identity_key(content: NodeContent, policy: IdentityPolicy) -> str:
    return json.dumps(
        identity_values(content, policy),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def identity_map(context, decision):
    mappings = dict(context.physical_map)
    used = set()
    old = {n.id for n in context.existing.graph.nodes}
    for group in decision.groups:
        members = set(group.members)
        if len(members) != len(group.members) or members & used:
            raise ValueError(
                "Identity groups must have distinct, non-overlapping members"
            )
        if not members <= context.read_ids:
            raise ValueError(
                f"Read identity members before deciding: {sorted(members - context.read_ids)}"
            )
        types = {context.nodes[key].node_type for key in members}
        if len(types) != 1 or types & PHYSICAL:
            raise ValueError(
                "Identity aliases require one semantic type; type correction is an explicit rewrite"
            )
        canonical = min(members, key=lambda key: (key not in old, key))
        mappings.update({key: canonical for key in members})
        used |= members
    distinctions = {frozenset(d.members) for d in decision.distinctions}
    for d in decision.distinctions:
        if len(set(d.members)) != 2 or not set(d.members) <= context.read_ids:
            raise ValueError("Distinction members must be two read semantic nodes")
        if mappings.get(d.members[0], d.members[0]) == mappings.get(
            d.members[1], d.members[1]
        ):
            raise ValueError("A pair cannot be both same and different identity")
    for a, b in context.mandatory:
        if (
            mappings.get(a, a) != mappings.get(b, b)
            and frozenset((a, b)) not in distinctions
        ):
            raise ValueError(
                f"Explicit identity decision needed for binding contest: {a}, {b}"
            )
    return mappings

