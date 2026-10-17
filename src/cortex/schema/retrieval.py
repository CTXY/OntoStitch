from __future__ import annotations

from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from cortex.schema.graph import GraphSpace
from cortex.schema.models import _SchemaModel


class RetrievalSourceDocument(_SchemaModel):
    document_id: str
    title: str
    source_path: str
    content: str = ""
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    outline: tuple[dict[str, Any], ...] = ()
    theme_id: str = ""
    theme_name: str = ""

    @field_validator("document_id", "title", "source_path")
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("retrieval document values must not be empty")
        return value


class RetrievalSection(_SchemaModel):
    section_id: str
    document_id: str
    chunk_id: str
    ordinal: int = Field(ge=0)
    title: str | None = None
    heading_path: tuple[str, ...] = ()
    text: str

    @field_validator("section_id", "document_id", "chunk_id")
    @classmethod
    def require_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("retrieval section identity must not be empty")
        return value

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("retrieval section text must not be empty")
        return value


class RetrievalUnit(_SchemaModel):
    unit_id: str
    representative_unit_id: str
    graph_node_id: str
    section_id: str
    title: str
    type_id: str
    summary: str = ""
    text: str
    is_primary: bool = False
    aliases: tuple[str, ...] = ()

    @field_validator(
        "unit_id",
        "representative_unit_id",
        "graph_node_id",
        "section_id",
        "title",
        "type_id",
    )
    @classmethod
    def require_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("retrieval unit values must not be empty")
        return value

    @field_validator("text")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("retrieval unit text must not be empty")
        return value


class RetrievalSnapshot(_SchemaModel):
    graph_run_id: str
    snapshot_fingerprint: str
    graph: GraphSpace
    documents: tuple[RetrievalSourceDocument, ...]
    sections: tuple[RetrievalSection, ...]
    units: tuple[RetrievalUnit, ...]

    @field_validator("graph_run_id", "snapshot_fingerprint")
    @classmethod
    def require_identity(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("retrieval snapshot identity must not be empty")
        return value

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        document_ids = _unique_ids(self.documents, "document_id", "document")
        section_ids = _unique_ids(self.sections, "section_id", "section")
        unit_ids = _unique_ids(self.units, "unit_id", "unit")
        chunk_ids = _unique_ids(self.sections, "chunk_id", "chunk")
        graph_node_ids = {node.id for node in self.graph.nodes}

        for section in self.sections:
            if section.document_id not in document_ids:
                raise ValueError(
                    f"retrieval section {section.section_id!r} references an unknown document"
                )
        for unit in self.units:
            if unit.section_id not in section_ids:
                raise ValueError(f"retrieval unit {unit.unit_id!r} references an unknown section")
            if unit.representative_unit_id not in unit_ids:
                raise ValueError(
                    f"retrieval unit {unit.unit_id!r} references an unknown representative"
                )
            if unit.graph_node_id not in graph_node_ids:
                raise ValueError(
                    f"retrieval unit {unit.unit_id!r} references an unknown graph node"
                )

        for item in (*self.graph.nodes, *self.graph.edges):
            for source in item.sources:
                if source.source_id not in document_ids:
                    raise ValueError(
                        f"graph source {source.source_id!r} references an unknown document"
                    )
                if source.chunk_id is not None and source.chunk_id not in chunk_ids:
                    raise ValueError(
                        f"graph source {source.source_id!r} references an unknown chunk "
                        f"{source.chunk_id!r}"
                    )
        for edge in self.graph.edges:
            if (
                edge.from_node_id not in graph_node_ids
                or edge.to_node_id not in graph_node_ids
            ):
                raise ValueError(f"graph edge {edge.id!r} references an unknown graph node")
        return self


def _unique_ids(items: tuple[object, ...], field: str, label: str) -> set[str]:
    values = [str(getattr(item, field)) for item in items]
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate retrieval {label} id")
    return set(values)


__all__ = [
    "RetrievalSection",
    "RetrievalSnapshot",
    "RetrievalSourceDocument",
    "RetrievalUnit",
]
