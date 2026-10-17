from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QARecord:
    question: str
    sql: str
    source_id: str
    database_id: str = "default"
    evidence: str = ""
    difficulty: str = ""


@dataclass(frozen=True)
class RetrievedExample:
    record: QARecord
    rank: int
    score: float


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]


@dataclass(frozen=True)
class SchemaSelection:
    ddl: str
    tables: dict[str, list[str]]
    value_matches: list[dict[str, Any]]
    mode: str
