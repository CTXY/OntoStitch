"""External dependencies of metadata initialization."""

from pathlib import Path
from typing import Protocol

from .models import MetadataSnapshot


class MetadataSource(Protocol):
    def scan(self) -> MetadataSnapshot: ...
    def query(self, sql: str) -> dict: ...
    def close(self) -> None: ...


class SnapshotRepository(Protocol):
    def check_source(self, source: str | Path) -> None: ...
    def load(self, revision: int | None = None) -> MetadataSnapshot: ...
    def save(
        self, snapshot: MetadataSnapshot, *, expected_revision: int
    ) -> MetadataSnapshot: ...
