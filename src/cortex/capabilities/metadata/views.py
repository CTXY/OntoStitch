"""Typed, object-scoped read API shared by agents and external clients."""

from __future__ import annotations

import json
from itertools import islice
from typing import Generic, Literal, TypeVar

from pydantic import Field, TypeAdapter

from .management import WRITABLE, MetadataChange
from .models import (
    Column,
    Database,
    MetadataSnapshot,
    Model,
    Relation,
    SourceInfo,
    Table,
    TableMetadata,
    column_reference,
    object_id,
)

ObjectKind = Literal["database", "table", "column", "relation"]
T = TypeVar("T")


class Page(Model, Generic[T]):
    items: list[T]
    total: int
    next_offset: int | None


class ObjectSummary(Model):
    id: str
    name: str
    kind: ObjectKind
    table_id: str | None = None
    description: str | None = None
    column_reference: str | None = None


class TableView(TableMetadata):
    columns: Page[ObjectSummary] = Field(
        description="Paginated column directory. Inspect each column ID for full metadata; no hidden recursive truncation."
    )


class SourcePage(SourceInfo):
    text: str
    next_offset: int | None


MODELS = {
    "database": Database,
    "table": TableView,
    "column": Column,
    "relation": Relation,
}


class MetadataReader:
    """Index object references once; serialize only the requested typed view."""

    def __init__(self, snapshot: MetadataSnapshot):
        self.snapshot = snapshot
        self.objects = {snapshot.database.id: snapshot.database}
        self.kinds = {snapshot.database.id: "database"}
        self.owners = {}
        for table in snapshot.tables:
            table_ref = object_id("table", table.name)
            self.objects[table_ref], self.kinds[table_ref] = table, "table"
            for column in table.columns:
                column_ref = object_id("column", table.name, column.name)
                self.objects[column_ref], self.kinds[column_ref] = column, "column"
                self.owners[column_ref] = table_ref
        for relation in snapshot.relations:
            self.objects[relation.id], self.kinds[relation.id] = relation, "relation"
        self.sources = {source.id: source for source in snapshot.sources}

    def _summary(self, key):
        obj = self.objects[key]
        return ObjectSummary(
            id=key,
            name=getattr(obj, "name", key),
            kind=self.kinds[key],
            table_id=self.owners.get(key),
            description=obj.description,
            column_reference=column_reference(
                self.objects[self.owners[key]].name, obj.name
            )
            if key in self.owners
            else None,
        )

    @staticmethod
    def _bounds(offset, limit):
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("offset >= 0 and 1 <= limit <= 50 required")

    def list(
        self,
        *,
        kind: ObjectKind = "table",
        table_id: str | None = None,
        offset=0,
        limit=20,
    ):
        self._bounds(offset, limit)
        if table_id is not None and (
            kind != "column" or self.kinds.get(table_id) != "table"
        ):
            raise ValueError(
                "table_id must identify a table and is only valid for a column directory"
            )
        ids = [
            key
            for key in self.objects
            if self.kinds[key] == kind
            and (table_id is None or self.owners.get(key) == table_id)
        ]
        return Page[ObjectSummary](
            items=[self._summary(key) for key in islice(ids, offset, offset + limit)],
            total=len(ids),
            next_offset=offset + limit if offset + limit < len(ids) else None,
        )

    def inspect(self, object_id: str, *, offset=0, limit=20):
        self._bounds(offset, limit)
        obj = self.objects.get(object_id)
        if obj is None:
            raise ValueError(f"UNKNOWN_OBJECT: {object_id}")
        if isinstance(obj, Table):
            # Read only table-level attributes. Never serialize owned columns first.
            return TableView(
                **{name: getattr(obj, name) for name in TableMetadata.model_fields},
                columns=self.list(
                    kind="column", table_id=object_id, offset=offset, limit=limit
                ),
            ).model_copy(deep=True)
        if offset or limit != 20:
            raise ValueError(
                "pagination on object details is only applicable to tables"
            )
        return obj.model_copy(deep=True)

    def inspect_many(self, object_ids: list[str], *, include_profiles=False):
        """Bounded batch view. Table columns remain explicitly paginated directories."""
        if not 1 <= len(object_ids) <= 20 or len(set(object_ids)) != len(object_ids):
            raise ValueError("Provide 1–20 distinct object addresses")
        items = {
            key: self.inspect(key).model_dump(
                mode="json", exclude=set() if include_profiles else {"profile"}
            )
            for key in object_ids
        }
        if len(json.dumps(items, ensure_ascii=False)) > 64000:
            raise ValueError(
                "BATCH_TOO_LARGE: reduce the batch or omit profiles; no results were truncated"
            )
        return {"items": items, "profiles_included": include_profiles}

    @staticmethod
    def schema(kind: ObjectKind | Literal["changes"]):
        if kind == "changes":
            return TypeAdapter(list[MetadataChange]).json_schema()
        model = MODELS[kind]
        writable_type = Table if kind == "table" else model
        return {
            "kind": kind,
            "writable_fields": sorted(WRITABLE.get(writable_type, set())),
            "json_schema": model.model_json_schema(),
        }

    def list_sources(self, *, offset=0, limit=20):
        self._bounds(offset, limit)
        sources = list(self.sources.values())
        return Page[SourceInfo](
            items=[
                SourceInfo(id=s.id, kind=s.kind, locator=s.locator)
                for s in sources[offset : offset + limit]
            ],
            total=len(sources),
            next_offset=offset + limit if offset + limit < len(sources) else None,
        )

    def read_source(self, source_ref: str, *, offset=0, limit=12000):
        if offset < 0 or not 1 <= limit <= 20000:
            raise ValueError("offset >= 0 and 1 <= limit <= 20000 required")
        source = self.sources.get(source_ref)
        if source is None:
            raise ValueError(f"UNKNOWN_SOURCE: {source_ref}")
        text = (
            source.content
            if isinstance(source.content, str)
            else json.dumps(source.content, ensure_ascii=False)
        )
        return SourcePage(
            id=source.id,
            kind=source.kind,
            locator=source.locator,
            text=text[offset : offset + limit],
            next_offset=offset + limit if offset + limit < len(text) else None,
        )
