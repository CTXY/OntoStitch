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
    references: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical Node IDs required by the condition or by the action's governed operand or "
            "target. They are all required together for the complete rule contract. Retrieval-only "
            "hints, replaceable implementation details, reason evidence, bare SQL identifiers, "
            "and case-coincident assets are invalid."
        ),
    )
    operation_intent: str | None = Field(
        default=None,
        description=(
            "Optional schema-independent discovery label. Initial Extraction uses a Registry "
            "intent; Repair may use one primary behavior kind. This field does not replace "
            "condition/action, define identity, or prove correctness."
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

    @field_validator("references")
    @classmethod
    def _validate_references(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_refs(value)))

    @field_validator("operation_intent", mode="before")
    @classmethod
    def _normalize_operation_intent(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("operation_intent must be a non-empty string or null")
        return value.strip()

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
            or self.references
        ):
            raise ValueError(
                "at least one question phrase, SQL Column, SQL operation, or ontology dependency "
                "is required"
            )
        return self
