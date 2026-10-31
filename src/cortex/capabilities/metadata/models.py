"""The versioned M0 contract. Unknown values stay null; extra fields are rejected."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_serializer,
    model_validator,
)

from .migration import migrate_snapshot
from .references import column_reference, object_id


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Profile(Model):
    observed_at: str
    snapshot_id: str
    scan_mode: Literal["full", "sample"]
    sampling_method: Literal["all_rows", "prefix"]
    scope_predicate: str | None = None
    rows_examined: int = Field(ge=0)


class TableProfile(Profile):
    row_count: int | None = Field(
        default=None,
        ge=0,
        description="Exact total only when scanned fully; never substitute sample size.",
    )


class TopValue(Model):
    value: JsonValue
    count: int = Field(ge=1)


class ColumnProfile(Profile):
    null_fraction: float | None = Field(default=None, ge=0, le=1)
    distinct_count: int = Field(
        ge=0,
        description="Distinct non-null physical values in scanned rows; text comparison is case-sensitive, independent of SQL collation.",
    )
    min: int | float | None = Field(
        default=None,
        description="Minimum finite numeric value; omitted for text, mixed-type or empty observations.",
    )
    max: int | float | None = Field(
        default=None,
        description="Maximum finite numeric value; omitted for text, mixed-type or empty observations.",
    )
    examples: list[JsonValue] = Field(default_factory=list)
    top_values: list[TopValue] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def omit_missing_bounds(self, handler):
        data = handler(self)
        for field in ("min", "max"):
            if data.get(field) is None:
                data.pop(field, None)
        return data


class SourceInfo(Model):
    """Small public reference to an original file, schema or database probe."""

    id: str = Field(description="System-assigned source reference; reuse it exactly.")
    kind: Literal["database_schema", "initial_file", "database_probe"]
    locator: str = Field(description="File path or SQL statement locating the source.")


class SourceRecord(SourceInfo):
    """Internal frozen source content. Omitted from ordinary metadata views."""

    content: JsonValue = None


class Column(Model):
    """One database field: meaning, stored representation and observations."""

    name: str = Field(
        description="Read-only original column name, including punctuation/case."
    )
    description: str | None = Field(
        default=None,
        description="Meaning and business role of the column, including repeated/coarser measures. Put code, NULL and special-value interpretations in value_description.",
    )
    value_description: str | None = Field(
        default=None,
        description="Plain-text interpretation of stored values: documented codes, NULL/sentinel meanings, ranges and conditional meanings. Preserve exact codes and conditions. Not a list of observed examples or an inferred exhaustive domain; unknown stays null.",
    )
    data_type: str = Field(
        description="Read-only declared database type, including precision if available."
    )
    nullable: bool | None = Field(
        default=None,
        description="Read-only DDL nullability; null means not established.",
    )
    default_expression: str | None = Field(
        default=None, description="Read-only declared SQL default expression."
    )
    representation: str | None = Field(
        default=None,
        description="Plain-text explanation of stored format, units and timezone, e.g. integer cents or YYYY-MM-DD text. Unknown details stay unspecified.",
    )
    profile: ColumnProfile | None = Field(
        default=None,
        description="Read-only statistics of a timestamped full scan or sample; not domain constraints.",
    )
    source_refs: list[str] = Field(
        default_factory=list,
        description="Sources consulted for this object; not property-level citations or proof of correctness. Maintained from edit source_refs.",
    )


class ForeignKey(Model):
    columns: list[str]
    referenced_table: str
    referenced_columns: list[str]


class TableMetadata(Model):
    """Table-level properties, without recursively embedding column metadata."""

    name: str = Field(description="Read-only original table or view name.")
    object_type: Literal["table", "view"] = Field(
        description="Read-only physical object type."
    )
    description: str | None = Field(
        default=None,
        description="What this table records; concise, without repeating column lists, keys or profile statistics.",
    )
    grain: str | None = Field(
        default=None,
        description="What one row represents, including event, item, version or time slice. A unique ID alone does not explain grain.",
    )
    scope: str | None = Field(
        default=None,
        description="Intended population and temporal storage mode, not just coverage seen in samples.",
    )
    primary_keys: list[str] = Field(
        default_factory=list,
        description="Read-only declared primary-key column names in order.",
    )
    unique_keys: list[list[str]] = Field(
        default_factory=list,
        description="Read-only declared unconditional unique column combinations.",
    )
    foreign_keys: list[ForeignKey] = Field(
        default_factory=list,
        description="Read-only declared foreign keys; composite keys are indivisible.",
    )
    profile: TableProfile | None = Field(
        default=None,
        description="Read-only timestamped table observations; sample size is not total row count.",
    )
    source_refs: list[str] = Field(
        default_factory=list,
        description="Sources consulted for this object; not property-level citations or proof of correctness. Maintained from edit source_refs.",
    )


class Table(TableMetadata):
    """Stored table with its owned column objects."""

    columns: list[Column] = Field(default_factory=list)


class Relation(Model):
    id: str
    type: Literal["join", "dependency", "derivation"]
    source_columns: list[str] = Field(
        min_length=1,
        description="Exact table.column names, no column. prefix or percent encoding; quote a component containing dots or double quotes.",
    )
    target_columns: list[str] = Field(
        min_length=1,
        description="Exact table.column names using the same convention as source_columns.",
    )
    expression: str | None = None
    description: str
    source_refs: list[str] = Field(
        default_factory=list,
        description="Sources consulted for this object; not property-level citations or proof of correctness. Maintained from edit source_refs.",
    )


class Database(Model):
    id: str
    name: str
    dialect: str = Field(default="sqlite", min_length=1)
    source: str
    snapshot_id: str
    description: str | None = Field(
        default=None,
        description="Concise overall data domain supported by tables or documentation; do not invent organization background.",
    )
    source_refs: list[str] = Field(
        default_factory=list,
        description="Sources consulted for this object; not property-level citations or proof of correctness. Maintained from edit source_refs.",
    )


class MetadataSnapshot(Model):
    schema_version: Literal["3.3"] = "3.3"
    revision: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    database: Database
    tables: list[Table]
    relations: list[Relation] = Field(default_factory=list)
    sources: list[SourceRecord] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy(cls, value):
        return migrate_snapshot(value)

    def public_dict(self):
        """Export source locators, not full files/query results or trace reasoning."""
        return self.model_dump(
            mode="json", exclude={"sources": {"__all__": {"content"}}}
        )

    def objects(self):
        objects = {self.database.id: self.database}
        for table in self.tables:
            objects[object_id("table", table.name)] = table
            for column in table.columns:
                objects[object_id("column", table.name, column.name)] = column
        objects.update({r.id: r for r in self.relations})
        return objects

    @model_validator(mode="after")
    def references_exist(self):
        objects = [
            self.database,
            *self.tables,
            *(c for t in self.tables for c in t.columns),
        ]
        ids = (
            [self.database.id]
            + [object_id("table", t.name) for t in self.tables]
            + [
                object_id("column", t.name, c.name)
                for t in self.tables
                for c in t.columns
            ]
            + [r.id for r in self.relations]
        )
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate metadata identity")
        source_ids = {source.id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source identity")
        table_ids = {t.name: t for t in self.tables}
        column_ids = {
            column_reference(t.name, c.name) for t in self.tables for c in t.columns
        }
        for obj in [*objects, *self.relations]:
            if not set(obj.source_refs) <= source_ids:
                raise ValueError("unknown source reference")
        for table in self.tables:
            local = {c.name for c in table.columns}
            for refs in [
                table.primary_keys,
                *table.unique_keys,
            ]:
                if refs is not None and (
                    not set(refs) <= local or len(refs) != len(set(refs))
                ):
                    raise ValueError("invalid table column reference")
            for fk in table.foreign_keys:
                target = table_ids.get(fk.referenced_table)
                if (
                    not target
                    or not fk.columns
                    or len(fk.columns) != len(fk.referenced_columns)
                    or not set(fk.columns) <= local
                    or not set(fk.referenced_columns)
                    <= {c.name for c in target.columns}
                ):
                    raise ValueError("invalid foreign key")
        for relation in self.relations:
            if not set(relation.source_columns + relation.target_columns) <= column_ids:
                raise ValueError("unknown relation column")
        return self


class BuildOptions(Model):
    profile_rows: int = Field(default=10000, ge=1, le=1000000)
    examples: int = Field(default=5, ge=0, le=50)
    query_timeout: float = Field(default=10, gt=0, le=300)
    max_agent_calls: int = Field(
        default=60,
        ge=1,
        le=100,
        description="Per Agent run (file interpretation or database enrichment).",
    )
    max_tool_calls: int = Field(
        default=240,
        ge=1,
        le=500,
        description="Per Agent run (file interpretation or database enrichment).",
    )
    context: str | None = None
