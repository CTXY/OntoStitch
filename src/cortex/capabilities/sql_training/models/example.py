from __future__ import annotations

from pydantic import Field, field_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel


class ExampleProperties(NodeModel):
    task: str
    sql: str | None = None
    generated_sql: str | None = None
    references: list[str] = Field(default_factory=list)

    _validate_references = field_validator("references")(normalize_refs)
