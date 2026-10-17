from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from cortex.capabilities.sql_training.core.ids import normalize_refs
from cortex.capabilities.sql_training.models.base import NodeModel


class DimensionValueProperties(NodeModel):
    references: list[str] = Field(default_factory=list)
    value: Any

    _validate_references = field_validator("references")(normalize_refs)
