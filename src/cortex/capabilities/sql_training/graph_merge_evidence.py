from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import SourceReference


class SqlTrainingEvidenceRepository(Protocol):
    def get(self, source_id: str) -> Mapping[str, Any] | None: ...


class SqlTrainingGraphMergeEvidenceResolver:
    """Resolve production-shaped SQL Training evidence without knowing acceptance layouts."""

    def __init__(self, repository: SqlTrainingEvidenceRepository):
        self.repository = repository

    def resolve(self, sources: tuple[SourceReference, ...]) -> tuple[EvidenceItem, ...]:
        evidence = []
        for source in sources:
            row = self.repository.get(source.source_id)
            if row is None:
                continue
            content = {
                key: row[key]
                for key in ("question", "sql", "revision", "validation")
                if key in row and row[key] is not None
            }
            evidence.append(
                EvidenceItem(
                    id=f"sql-training:{source.source_id}",
                    source=source,
                    case_id=source.case_id,
                    content=content,
                )
            )
        return tuple(evidence)


class MappingEvidenceRepository:
    def __init__(self, rows: Mapping[str, Mapping[str, Any]]):
        self.rows = rows

    def get(self, source_id: str) -> Mapping[str, Any] | None:
        return self.rows.get(source_id)


class WorkspaceEvidenceRepository:
    def __init__(self, workspace):
        self.rows = {
            str(row["source_id"]): row
            for row in workspace.read_source_evidence()
            if row.get("source_id")
        }

    def get(self, source_id: str) -> Mapping[str, Any] | None:
        return self.rows.get(source_id)


__all__ = [
    "MappingEvidenceRepository",
    "SqlTrainingGraphMergeEvidenceResolver",
    "WorkspaceEvidenceRepository",
]
