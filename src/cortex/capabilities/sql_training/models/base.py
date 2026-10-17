from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from cortex.capabilities.sql_training.core.enums import SourceType


class NodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(NodeModel):
    source_type: SourceType
    source_id: str | None = None


def _uppercase(value: Any) -> Any:
    return value.upper() if isinstance(value, str) else value
