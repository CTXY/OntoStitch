from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import make_node_id, normalize_ref, normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel


class MetricExpression(NodeModel):
    expr: str = Field(
        description="Complete reusable formula for the named Metric, using ${key} placeholders."
    )
    refs: dict[str, str] = Field(
        default_factory=dict,
        description="Map each bare placeholder key to one canonical physical or Metric Node ID.",
    )

    @field_validator("refs")
    @classmethod
    def _validate_refs(cls, values: dict[str, str]) -> dict[str, str]:
        return {key: normalize_ref(value) for key, value in values.items()}

    @model_validator(mode="after")
    def _validate_bindings(self) -> MetricExpression:
        placeholders = set(re.findall(r"\$\{([^{}]+)\}", self.expr))
        if not placeholders:
            return self
        ref_keys = set(self.refs)
        if missing := placeholders - ref_keys:
            raise ValueError(f"expression placeholders have no typed reference: {sorted(missing)}")
        if unused := ref_keys - placeholders:
            raise ValueError(f"expression refs have no placeholder: {sorted(unused)}")
        return self


class SQLGenerationPlan(NodeModel):
    """Reusable physical calculation guidance for one selectable Metric path."""

    id: str = Field(description="Stable plan-local identifier; not an ontology Node ID.")
    application: str = Field(
        description="When this physical implementation path applies, without current literals."
    )
    steps: list[str] = Field(
        min_length=1,
        max_length=8,
        description="Ordered physical steps that implement the Metric expression exactly.",
    )

    @model_validator(mode="after")
    def _validate_non_empty_text(self) -> SQLGenerationPlan:
        plan_id = self.id.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", plan_id):
            raise ValueError(
                "sql_generation_plans id must start with a letter and contain only "
                "letters, digits, or underscores"
            )
        application = self.application.strip()
        if not application:
            raise ValueError("sql_generation_plans application must be non-empty")
        if len(application) > 500:
            raise ValueError("sql_generation_plans application must be at most 500 characters")
        cleaned = [step.strip() for step in self.steps]
        if any(not step for step in cleaned):
            raise ValueError("sql_generation_plans steps must be non-empty strings")
        if any(len(step) > 600 for step in cleaned):
            raise ValueError("sql_generation_plans steps must be at most 600 characters")
        self.id = plan_id
        self.application = application
        self.steps = cleaned
        return self

    def schema_references(self) -> list[str]:
        return metric_text_schema_references([self.application, *self.steps])


def metric_text_schema_references(texts: list[str]) -> list[str]:
    refs: list[str] = []
    for text in texts:
        refs.extend(
            make_node_id(NodeType.TABLE, table)
            for table in re.findall(r"<([^<>\s.]+)>", text)
            if "." not in table
        )
        refs.extend(
            make_node_id(NodeType.COLUMN, table, column)
            for table, column in re.findall(r"<([^<>\s.]+)\.([^<>.]+)>", text)
        )
    return list(dict.fromkeys(refs))


class MetricProperties(NodeModel):
    references: list[str] = Field(
        default_factory=list,
        description="Exact canonical dependency closure used by expression.refs.",
    )
    metric_kind: str | None = Field(
        default=None,
        description="Optional classification of the measured quantity; not its identity.",
    )
    pattern: str = Field(
        default="",
        description="Concise natural-language statement of the same formula as expression.",
    )
    expression: MetricExpression = Field(
        description="The intrinsic calculation that defines the complete Metric term."
    )
    aggregation_policy: Literal[
        "DIRECT_AGGREGATE",
        "POST_AGGREGATE_CALC",
        "AGGREGATE_THEN_JOIN",
    ] | None = Field(
        default=None,
        description="Overall aggregation shape used to realize the Metric formula.",
    )
    calculation_policy: dict[str, Any] = Field(
        default_factory=dict,
        description="Only calculation policies intrinsic to the named Metric.",
    )
    sql_generation_plans: list[SQLGenerationPlan] = Field(
        default_factory=list,
        description="Reusable physical implementation paths for the Metric expression.",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        cleaned = dict(value)
        cleaned.pop("physical_dependencies", None)
        legacy_dimensions = {
            str(item)
            for item in cleaned.pop("allowed_dimension_refs", []) or []
            if isinstance(item, str)
        }
        legacy_default_time = cleaned.pop("default_time", None)
        if isinstance(legacy_default_time, str):
            legacy_dimensions.add(legacy_default_time)
        filters = cleaned.pop("filters", []) or []
        if filters:
            _fold_legacy_filters_into_expression(cleaned, filters)
        expression = cleaned.get("expression")
        expression_refs = (
            set(expression.get("refs", {}).values())
            if isinstance(expression, dict) and isinstance(expression.get("refs"), dict)
            else set()
        )
        references = list(cleaned.get("references") or [])
        cleaned["references"] = [
            item for item in references if item not in legacy_dimensions or item in expression_refs
        ]
        return cleaned

    _validate_references = field_validator("references")(normalize_refs)

    @field_validator("pattern")
    @classmethod
    def _normalize_pattern(cls, value: str) -> str:
        pattern = value.strip()
        if len(pattern) > 4000:
            raise ValueError("Metric pattern must be at most 4000 characters")
        return pattern

    @field_validator("sql_generation_plans", mode="before")
    @classmethod
    def _normalize_empty_sql_generation_plans(cls, value: Any) -> Any:
        return [] if value is None else value


def _fold_legacy_filters_into_expression(
    properties: dict[str, Any], filters: list[Any]
) -> None:
    expression = properties.get("expression")
    if not isinstance(expression, dict):
        return
    refs = dict(expression.get("refs") or {})
    declared = list(properties.get("references") or [])
    predicates: list[str] = []
    for index, raw_filter in enumerate(filters, start=1):
        if not isinstance(raw_filter, dict):
            continue
        column_ref = raw_filter.get("column_ref")
        if not isinstance(column_ref, str) or not column_ref.strip():
            continue
        column_key = f"intrinsic_filter_{index}_column"
        refs[column_key] = column_ref
        declared.append(column_ref)
        operator = str(raw_filter.get("operator") or "=").replace("_", " ")
        value_refs = raw_filter.get("value_refs")
        if not isinstance(value_refs, list):
            value_ref = raw_filter.get("value_ref")
            value_refs = [value_ref] if isinstance(value_ref, str) else []
        operands: list[str] = []
        for value_index, value_ref in enumerate(value_refs, start=1):
            if not isinstance(value_ref, str) or not value_ref.strip():
                continue
            value_key = f"intrinsic_filter_{index}_value_{value_index}"
            refs[value_key] = value_ref
            declared.append(value_ref)
            operands.append(f"${{{value_key}}}")
        if not operands and raw_filter.get("value") is not None:
            operands.append(repr(raw_filter["value"]))
        left = f"${{{column_key}}}"
        if operator in {"IS NULL", "IS NOT NULL"}:
            predicates.append(f"{left} {operator}")
        elif operator in {"IN", "NOT IN"}:
            predicates.append(f"{left} {operator} ({', '.join(operands)})")
        elif operands:
            predicates.append(f"{left} {operator} {operands[0]}")
    if not predicates:
        return
    base_expression = str(expression.get("expr") or "").strip()
    expression["expr"] = f"FILTER(({base_expression}), {' AND '.join(predicates)})"
    expression["refs"] = refs
    properties["expression"] = expression
    properties["references"] = list(dict.fromkeys(declared))
