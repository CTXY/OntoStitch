from __future__ import annotations

import re

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.enums import NodeType
from cortex.capabilities.sql_training.core.ids import (
    make_node_id,
    normalize_ref,
)
from cortex.capabilities.sql_training.models.base import NodeModel
from cortex.schema.semantic_validation import validate_semantic_properties


class MetricExpression(NodeModel):
    expr: str = Field(
        description="Complete reusable formula for the named Metric, using ${key} placeholders."
    )
    refs: dict[str, str] = Field(
        default_factory=dict,
        description="Map each bare placeholder key to one canonical Concept, Metric or physical Node ID.",
    )

    @field_validator("refs")
    @classmethod
    def _validate_refs(cls, values: dict[str, str]) -> dict[str, str]:
        return {key: normalize_ref(value) for key, value in values.items()}

    @model_validator(mode="after")
    def _validate_bindings(self) -> MetricExpression:
        validate_semantic_properties(
            "metric",
            {
                "expression": self.model_dump(mode="json"),
            },
        )
        return self


class SQLGenerationPlan(NodeModel):
    """Reusable physical calculation guidance for one selectable Metric path."""

    id: str = Field(
        description="Stable plan-local identifier; not an ontology Node ID."
    )
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
            raise ValueError(
                "sql_generation_plans application must be at most 500 characters"
            )
        cleaned = [step.strip() for step in self.steps]
        if any(not step for step in cleaned):
            raise ValueError("sql_generation_plans steps must be non-empty strings")
        if any(len(step) > 600 for step in cleaned):
            raise ValueError(
                "sql_generation_plans steps must be at most 600 characters"
            )
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
    pattern: str = Field(
        default="",
        description="Concise natural-language statement of the same formula as expression.",
    )
    expression: MetricExpression = Field(
        description="The defining formula of an independently meaningful business quantity. A generic aggregate of an existing population is a query operation, not a Metric; a valid expression alone is insufficient."
    )
    sql_generation_plans: list[SQLGenerationPlan] = Field(
        default_factory=list,
        description="Reusable physical implementation paths for the Metric expression.",
    )

    @field_validator("pattern")
    @classmethod
    def _normalize_pattern(cls, value: str) -> str:
        pattern = value.strip()
        if len(pattern) > 4000:
            raise ValueError("Metric pattern must be at most 4000 characters")
        return pattern

    @model_validator(mode="after")
    def _validate_content(self) -> MetricProperties:
        validate_semantic_properties("metric", self.model_dump(mode="json"))
        return self
