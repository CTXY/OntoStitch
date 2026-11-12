"""Dataset loader contract. Loaders propose changes; the service validates writes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

from .management import MetadataChange
from .models import MetadataSnapshot, SourceRecord


@dataclass
class LoaderResult:
    """Atomic proposal batches, plus optional loader-model diagnostics."""

    batches: list[list[MetadataChange]] = field(default_factory=list)
    supported: bool = True
    issues: list[str] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    status: Literal["completed", "incomplete"] = "completed"


class MetadataLoader(ABC):
    """Interpret one supplied file using an explicitly selected dataset contract.

    Never mutate snapshot. Preserve exact object identities and source references.
    Report unsupported input or invalid records in issues, retaining valid records.
    The service owns conflict detection, semantic validation and persistence.
    """

    @abstractmethod
    def extract(self, snapshot: MetadataSnapshot, source: SourceRecord) -> LoaderResult:
        raise NotImplementedError
