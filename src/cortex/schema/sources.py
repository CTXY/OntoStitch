from __future__ import annotations

from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from cortex.schema.models import _SchemaModel


class SourceDocument(_SchemaModel):
    id: str
    path: str
    title: str | None = None
    media_type: str = "text/plain"
    content_hash: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", "path", "content_hash")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("source document values must not be empty")
        return value


class SourceSection(_SchemaModel):
    id: str
    document_id: str
    ordinal: int = Field(ge=0)
    title: str | None = None
    parent_section_id: str | None = None


class SourceChunk(_SchemaModel):
    id: str
    document_id: str
    section_id: str | None = None
    ordinal: int = Field(ge=0)
    text: str
    content_hash: str

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source chunk text must not be empty")
        return value


class SourceCorpus(_SchemaModel):
    documents: tuple[SourceDocument, ...] = ()
    sections: tuple[SourceSection, ...] = ()
    chunks: tuple[SourceChunk, ...] = ()

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        document_ids = _unique_ids(self.documents, "document")
        section_ids = _unique_ids(self.sections, "section")
        _unique_ids(self.chunks, "chunk")
        for section in self.sections:
            if section.document_id not in document_ids:
                raise ValueError(f"section {section.id!r} references an unknown document")
            if section.parent_section_id and section.parent_section_id not in section_ids:
                raise ValueError(f"section {section.id!r} references an unknown parent section")
        for chunk in self.chunks:
            if chunk.document_id not in document_ids:
                raise ValueError(f"chunk {chunk.id!r} references an unknown document")
            if chunk.section_id and chunk.section_id not in section_ids:
                raise ValueError(f"chunk {chunk.id!r} references an unknown section")
        return self


def _unique_ids(items: tuple[object, ...], label: str) -> set[str]:
    values = [str(item.id) for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label} id")
    return set(values)
