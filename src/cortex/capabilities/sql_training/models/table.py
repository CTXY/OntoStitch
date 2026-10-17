from __future__ import annotations

from pydantic import Field

from cortex.capabilities.sql_training.models.base import NodeModel


class TableProperties(NodeModel):
    database: str | None = None
    database_description: str | None = None
    table_name: str
    table_schema: str | None = None
    important_columns: list[str] = Field(default_factory=list)
    primary_keys: list[str] = Field(default_factory=list)
    foreign_keys: list[str] = Field(default_factory=list)
    available_time_columns: list[str] = Field(default_factory=list)
    default_time_column: str | None = None
