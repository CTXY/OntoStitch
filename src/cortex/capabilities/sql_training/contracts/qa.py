from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class QAInput(BaseModel):
    """Canonical QA-SQL input consumed by Cortex pipelines."""

    model_config = ConfigDict(extra="allow")

    question: str
    sql: str
    source_id: str
    evidence: str | None = Field(
        default=None,
        description=(
            "Optional dataset evidence that explains Question terms or their Gold SQL binding."
        ),
    )
    generated_sql: str | None = None
    assets: list[Any] = Field(default_factory=list)
    metrics: list[Any] = Field(default_factory=list)
    semantic_mappings: list[Any] = Field(default_factory=list)
    generation_rules: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def reject_unsupported_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            unsupported = sorted({"id", "sourceid", "evaluation"} & set(value))
            if unsupported:
                raise ValueError(f"unsupported input fields: {unsupported}")
        return value

    @field_validator("question", "sql", "source_id", mode="before")
    @classmethod
    def normalize_required_text(cls, value: Any) -> str:
        return str(value).strip() if value is not None else ""

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_optional_evidence(cls, value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @field_validator("question", "sql", "source_id")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value

    def to_record(self) -> dict[str, Any]:
        """Return the stable file/prompt representation without empty optional lists."""
        return self.model_dump(mode="json", exclude_defaults=True, exclude_none=True)


__all__ = ["QAInput"]
