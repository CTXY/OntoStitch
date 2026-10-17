from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel


def semantic_filter_leaves(value: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    operator = str(value.get("operator") or "").strip().casefold()
    if operator in {"and", "or"}:
        operands = value.get("operands")
        if not isinstance(operands, list):
            return []
        return [
            leaf
            for operand in operands
            if isinstance(operand, dict)
            for leaf in semantic_filter_leaves(operand)
        ]
    return [value]


def semantic_filter_refs(value: dict[str, Any] | None) -> list[str]:
    refs: list[str] = []
    for leaf in semantic_filter_leaves(value):
        for key in ("column_ref", "value_ref"):
            ref = leaf.get(key)
            if isinstance(ref, str) and ref.strip():
                refs.append(ref.strip())
        value_refs = leaf.get("value_refs")
        if isinstance(value_refs, list):
            refs.extend(
                str(ref).strip() for ref in value_refs if isinstance(ref, str) and ref.strip()
            )
    return list(dict.fromkeys(refs))


class SemanticMappingTarget(NodeModel):
    id: str = Field(description="Stable target-local identifier; not an ontology Node ID.")
    sql_expression: str = Field(
        description="Executable source-local expression that directly returns or tests the Concept."
    )
    references: list[str] = Field(
        default_factory=list,
        description="Exact physical Column and governed Value IDs used by sql_expression.",
    )
    binding_operator: Literal["exact", "one_of", "all_of"] = Field(
        default="exact",
        description=(
            "How the target's semantic endpoints represent this one Concept after separating "
            "supporting Columns: exact means exactly one endpoint realizes the Concept; all_of "
            "means every endpoint is jointly required and removing any endpoint makes the "
            "Concept incomplete; one_of means the endpoints are alternatives and any one "
            "endpoint can realize the same Concept. Never use exact when more than one "
            "semantic endpoint remains."
        ),
    )

    _validate_references = field_validator("references")(normalize_refs)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_target(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        operator = str(normalized.get("binding_operator") or "").strip().casefold()
        if operator in {"=", "or", "and"}:
            normalized["binding_operator"] = {
                "=": "exact",
                "or": "one_of",
                "and": "all_of",
            }[operator]
        template = backfill_filter_ids(normalized.pop("filter_template", None))
        semantic_targets = normalized.pop("semantic_targets", None)
        implementation = normalized.pop("implementation", None)
        references = list(normalize_refs(normalized.get("references") or []))
        if isinstance(semantic_targets, list):
            references.extend(
                str(item.get("node_id"))
                for item in semantic_targets
                if isinstance(item, dict) and item.get("node_id")
            )
        if isinstance(implementation, dict):
            references.extend(
                str(item)
                for item in implementation.get("references", [])
                if isinstance(item, str) and item.strip()
            )
        references.extend(semantic_filter_refs(template))
        normalized["references"] = list(dict.fromkeys(normalize_refs(references)))
        if not normalized.get("id"):
            normalized["id"] = stable_mapping_id(normalized, prefix="legacy")
        endpoints = semantic_endpoint_references(normalized["references"])
        if len(endpoints) > 1 and not normalized.get("binding_operator"):
            template_operator = (
                str(template.get("operator") or "").strip().casefold()
                if isinstance(template, dict)
                else ""
            )
            normalized["binding_operator"] = (
                "all_of" if template_operator == "and" else "one_of"
            )
        if (
            len(endpoints) > 1
            and normalized.get("binding_operator") == "exact"
            and _same_column_dimension_values(endpoints)
        ):
            normalized["binding_operator"] = "one_of"
        if len(endpoints) == 1 and normalized.get("binding_operator") in {
            "one_of",
            "all_of",
        }:
            normalized["binding_operator"] = "exact"
        return normalized

    @model_validator(mode="after")
    def validate_target(self) -> SemanticMappingTarget:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", self.id):
            raise ValueError(
                "Semantic Mapping target id must start with a letter and contain only "
                "letters, digits, or underscores"
            )
        if not self.sql_expression.strip():
            raise ValueError("Semantic Mapping target sql_expression must be non-empty")
        endpoints = semantic_endpoint_references(self.references)
        if self.references and not endpoints:
            raise ValueError("Semantic Mapping target requires a physical semantic endpoint")
        if len(endpoints) == 1 and self.binding_operator != "exact":
            raise ValueError(
                "one Semantic Mapping endpoint requires binding_operator exact"
            )
        if len(endpoints) > 1 and self.binding_operator == "exact":
            raise ValueError(
                "multiple Semantic Mapping endpoints require an explicit combination operator"
            )
        return self


class ConceptProperties(NodeModel):
    mapping_type: str | None = Field(
        default=None,
        description="Optional mapping classification; it does not define Concept identity.",
    )
    targets: list[SemanticMappingTarget] = Field(
        default_factory=list,
        description=(
            "Source-local physical realizations of the same atomic Concept. Every target must "
            "fully implement that same meaning."
        ),
    )

    @model_validator(mode="after")
    def validate_mapping(self) -> ConceptProperties:
        fingerprints = [
            (
                target.sql_expression.strip().casefold(),
                tuple(sorted(target.references)),
            )
            for target in self.targets
        ]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Semantic Mapping contains duplicate targets")
        target_ids = [target.id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("Semantic Mapping contains duplicate target ids")
        return self


def semantic_endpoint_references(references: list[str]) -> list[str]:
    value_refs = [ref for ref in references if ref.startswith("dimension_value.")]
    if value_refs:
        return value_refs
    return [ref for ref in references if ref.startswith("column.")]


def _same_column_dimension_values(references: list[str]) -> bool:
    if not references or any(not ref.startswith("dimension_value.") for ref in references):
        return False
    prefixes = {
        ".".join(ref.removeprefix("dimension_value.").split(".")[:2])
        for ref in references
    }
    return len(prefixes) == 1


def _validate_filter_template(value: dict[str, Any]) -> None:
    legacy_fields = sorted({"ref", "value_expr", "value_type"}.intersection(value))
    if legacy_fields:
        raise ValueError(
            "Semantic Mapping filter_template uses unsupported legacy fields: "
            + ", ".join(legacy_fields)
        )
    operator = str(value.get("operator") or "").strip().casefold()
    if operator in {"and", "or"}:
        operands = value.get("operands")
        if not isinstance(operands, list) or len(operands) < 2:
            raise ValueError(
                "Semantic Mapping AND/OR filter_template requires at least two operands"
            )
        for operand in operands:
            if not isinstance(operand, dict):
                raise ValueError("Semantic Mapping filter_template operands must be objects")
            _validate_filter_template(operand)
        return

    leaf_id = value.get("id")
    if not isinstance(leaf_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", leaf_id):
        raise ValueError("Semantic Mapping filter leaf requires a stable id")

    column_ref = value.get("column_ref")
    value_ref = value.get("value_ref")
    has_literal_value = "value" in value and value.get("value") is not None
    if column_ref is not None and (
        not isinstance(column_ref, str) or not column_ref.startswith("column.")
    ):
        raise ValueError("Semantic Mapping filter_template.column_ref must be a column ref")
    if value_ref is not None and (
        not isinstance(value_ref, str) or not value_ref.startswith("dimension_value.")
    ):
        raise ValueError("Semantic Mapping filter_template.value_ref must be a dimension_value ref")
    if isinstance(column_ref, str) and isinstance(value_ref, str):
        expected_prefix = f"dimension_value.{column_ref.removeprefix('column.')}."
        if not value_ref.startswith(expected_prefix):
            raise ValueError(
                "Semantic Mapping filter_template value_ref does not belong to column_ref"
            )
    if operator in {"=", "eq"}:
        if not column_ref or (bool(value_ref) == has_literal_value):
            raise ValueError(
                "Semantic Mapping equality filter_template requires column_ref and exactly "
                "one of value_ref or value"
            )
    if operator == "like" and isinstance(column_ref, str) and "pattern" not in value:
        raise ValueError("Semantic Mapping LIKE filter_template requires pattern")


def stable_mapping_id(value: dict[str, Any], *, prefix: str = "legacy") -> str:
    payload = {key: item for key, item in value.items() if key != "id"}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def backfill_filter_ids(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    operator = str(normalized.get("operator") or "").strip().casefold()
    if operator in {"and", "or"}:
        operands = normalized.get("operands")
        if isinstance(operands, list):
            normalized["operands"] = [
                backfill_filter_ids(operand) if isinstance(operand, dict) else operand
                for operand in operands
            ]
        return normalized
    normalized.setdefault("id", stable_mapping_id(normalized, prefix="predicate"))
    return normalized
