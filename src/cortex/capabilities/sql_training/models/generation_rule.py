from __future__ import annotations

import re
from typing import Any

from pydantic import Field, field_validator, model_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel


def _normalize_strings(value: Any, *, lowercase: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("value must be a list of strings")  # noqa: TRY004 - Pydantic field feedback.
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("value must be a list of strings")  # noqa: TRY004 - Pydantic field feedback.
        text = re.sub(r"\s+", " ", item.strip())
        if lowercase:
            text = text.lower()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            normalized.append(text)
    return normalized


class QuestionTrigger(NodeModel):
    phrases: list[str] = Field(
        default_factory=list,
        description=(
            "Compact phrases in the same language as the question. Each phrase is an independent "
            "OR retrieval signal and should materially increase the likelihood that the rule "
            "applies."
        ),
    )

    @field_validator("phrases", mode="before")
    @classmethod
    def _normalize_phrases(cls, value: Any) -> list[str]:
        return _normalize_strings(value)


class SqlTrigger(NodeModel):
    columns: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical Column IDs whose appearance in current or incorrect SQL strongly predicts "
            "that this rule may be relevant. Each Column is an independent OR retrieval signal; "
            "the rule condition still decides exact applicability."
        ),
    )
    operations: list[str] = Field(
        default_factory=list,
        description=(
            "Lowercase SQL operations or short behavior fragments whose appearance in an "
            "incorrect draft strongly predicts that this rule may be relevant. Each operation is "
            "an independent OR retrieval signal; broad non-predictive syntax is invalid."
        ),
    )

    @field_validator("columns")
    @classmethod
    def _normalize_columns(cls, value: list[str]) -> list[str]:
        columns = list(dict.fromkeys(normalize_refs(value)))
        invalid = [
            reference for reference in columns if not reference.startswith("column.")
        ]
        if invalid:
            raise ValueError(
                f"SQL trigger columns must be canonical Column IDs: {invalid}"
            )
        return columns

    @field_validator("operations", mode="before")
    @classmethod
    def _normalize_operations(cls, value: Any) -> list[str]:
        return _normalize_strings(value, lowercase=True)


class GenerationRuleTriggers(NodeModel):
    question: QuestionTrigger = Field(default_factory=QuestionTrigger)
    sql: SqlTrigger = Field(default_factory=SqlTrigger)


class GenerationRuleProperties(NodeModel):
    operation_intents: list[str] = Field(
        default_factory=list,
        description=(
            "Reusable intent labels used to organize the atomic action. Consult the operation "
            "registry, reuse matching labels, or propose a new label explained in reason. "
            "Multiple labels do not justify combining independent actions."
        ),
    )
    triggers: GenerationRuleTriggers = Field(
        description=(
            "Independent OR retrieval signals. Question phrases, SQL Columns, and SQL operations "
            "each indicate that the rule may be relevant; none proves the condition. Initial "
            "Extraction must leave SQL-side triggers empty."
        )
    )
    condition: str = Field(
        description=(
            "Smallest pre-decision runtime business request that requires the whole action, not a "
            "Question summary. Never state the required representation, chosen operation, direct "
            "result, postcondition, or contrast conclusion as a condition fact. SQL triggers are "
            "retrieval signals, not condition facts."
        )
    )
    action: str = Field(
        description=(
            "One causally connected SQL-generation behavior. Name necessary SQL operations and "
            "physical bindings. When prose alone does not fix the operation choice or order, "
            "include a local SQL pattern with role-labelled placeholders whose business roles "
            "are explained. Keep multiple operations only when all implement the "
            "same result-changing behavior. Required/Avoid text must contrast the runtime input "
            "source and direct result, without current values, aliases, or ontology IDs. Avoid "
            "text must not also describe the required Gold behavior."
        )
    )
    reason: str = Field(
        description=(
            "Concise observable difference between the action and another plausible "
            "implementation of the same requested operation. Action and reason must assign "
            "the same rows, cardinality, order, ties, values, and NULL behavior to the stated "
            "action; a different outcome belongs only to the contrasted alternative."
        )
    )

    @field_validator("operation_intents", mode="before")
    @classmethod
    def _normalize_operation_intents(cls, value: Any) -> list[str]:
        return _normalize_strings(value)

    @field_validator("condition", "action", "reason", mode="before")
    @classmethod
    def _normalize_rule_text(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("condition, action, and reason must be non-empty strings")
        return value.strip()

    @model_validator(mode="after")
    def _validate_retrieval_signal(self) -> GenerationRuleProperties:
        if not (
            self.triggers.question.phrases
            or self.triggers.sql.columns
            or self.triggers.sql.operations
        ):
            raise ValueError(
                "at least one question phrase, SQL Column, or SQL operation "
                "is required"
            )
        return self
