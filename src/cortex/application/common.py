from __future__ import annotations

from collections.abc import Awaitable
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from cortex.schema.sources import SourceCorpus


class RunStatus(StrEnum):
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    severity: ReviewSeverity = ReviewSeverity.WARNING
    target: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RunDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(default_factory=lambda: str(uuid4()))
    config_fingerprint: str | None = None
    metrics: dict[str, int | float | str | bool] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class ApplicationError(RuntimeError):
    """Base error for protocol-independent Cortex use cases."""


class SourceLoadError(ApplicationError):
    pass


class DocumentLoader(Protocol):
    def load(self, paths: tuple[Path, ...]) -> SourceCorpus | Awaitable[SourceCorpus]: ...


__all__ = [
    "ApplicationError",
    "DocumentLoader",
    "ReviewItem",
    "ReviewSeverity",
    "RunDiagnostics",
    "RunStatus",
    "SourceLoadError",
]
