from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from cortex.capabilities.sql_training.core.enums import NodeType


@dataclass(frozen=True)
class NodeId:
    type: NodeType
    name: str

    def __str__(self) -> str:
        return f"{self.type.value}.{self.name}"


def parse_node_id(value: str) -> NodeId:
    if not isinstance(value, str) or "." not in value:
        raise ValueError(f"NODE IDs must use dot form '<type>.<name>': {value!r}")
    raw_type, name = value.split(".", 1)
    if not raw_type or not name:
        raise ValueError(f"NODE ID type and name must be non-empty: {value!r}")
    try:
        node_type = NodeType(raw_type)
    except ValueError as exc:
        raise ValueError(f"Unknown NODE ID type: {raw_type!r}") from exc
    return NodeId(type=node_type, name=name)


def make_node_id(node_type: NodeType | str, *parts: Any) -> str:
    normalized_type = NodeType(node_type)
    name = ".".join(stable_slug(part) for part in parts if _has_node_id_part(part))
    if not name:
        raise ValueError("NODE ID requires at least one non-empty name part")
    return str(NodeId(type=normalized_type, name=name))


def make_generation_rule_id(
    *,
    rule_name: Any = None,
    question_phrases: Any = None,
    sql_operations: Any = None,
    references: Any = None,
    condition: Any = None,
) -> str:
    rule_name_text = re.sub(r"\s+", " ", str(rule_name or "").strip())
    if not rule_name_text:
        raise ValueError("Generation Rule CREATE requires a stable behavior name")
    return make_node_id(NodeType.GENERATION_RULE, stable_slug(rule_name_text, with_hash=True))


def stable_slug(text: Any, *, max_length: int = 120, with_hash: bool = False) -> str:
    raw = "" if text is None else str(text).strip()
    slug = re.sub(r"\s+", "_", raw)
    slug = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", slug).strip("_")
    slug = slug[:max_length].strip("_") or "unnamed"
    if with_hash:
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}_{digest}"
    return slug


def _has_node_id_part(part: Any) -> bool:
    return part is not None and str(part).strip() != ""


def _normalized_identity_list(value: Any, *, lowercase: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "").strip())
        if lowercase:
            text = text.lower()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def normalize_ref(value: Any, *, expected_type: NodeType | str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("reference must be a non-empty string")
    text = value.strip()
    parsed = parse_node_id(text)
    if expected_type is not None and parsed.type != NodeType(expected_type):
        raise ValueError(f"reference {text!r} must be a {NodeType(expected_type).value} reference")
    return text


def normalize_refs(values: list[str]) -> list[str]:
    return [normalize_ref(value) for value in values]
