from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class TableProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(validation_alias=AliasChoices("name", "table_name"))
    schema_name: str | None = None
    row_count: int | None = None
    database: str | None = None
    comment: str | None = None
    display_name: str | None = None
    description: str | None = None
    primary_keys: list[str] = Field(default_factory=list)
    foreign_keys: list[str] = Field(default_factory=list)

    @property
    def table_name(self) -> str:
        return self.name


class ColumnProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_name: str
    column_name: str
    data_type: str | None = None
    nullable: bool | None = None
    default: str | None = None
    comment: str | None = None
    sample_values: list[Any] = Field(default_factory=list)
    primary_key: bool = False
    foreign_key: bool = False


class JoinProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_table: str
    to_table: str
    column_pairs: list[tuple[str, str]] = Field(default_factory=list)
    constraint_name: str | None = None


class CatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableProfile] = Field(default_factory=list)
    columns: list[ColumnProfile] = Field(default_factory=list)
    joins: list[JoinProfile] = Field(default_factory=list)


class ColumnValueStats(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    non_empty_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)


__all__ = [
    "CatalogSnapshot",
    "ColumnProfile",
    "ColumnValueStats",
    "JoinProfile",
    "TableProfile",
]
